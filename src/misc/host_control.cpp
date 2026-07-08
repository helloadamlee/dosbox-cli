#include "host_control.h"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <fcntl.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#endif

#include "dosbox.h"
#include "bios.h"
#include "control.h"
#include "dos_inc.h"
#include "shell.h"

namespace host_control {
namespace {

constexpr std::size_t buffered_output_max_bytes = 4096;
constexpr uint64_t buffered_output_max_ms = 100;
constexpr std::size_t input_queue_max_codes = 1024;

struct PendingInputCode {
	uint64_t sequence = 0;
	uint16_t code = 0;
};

bool session_active = false;
std::atomic<bool> session_write_failed = {false};
std::string active_request_id = {};
BufferedOutput buffered_output = {};
WriteLineFn active_write_line = {};
std::deque<PendingInputCode> pending_input_codes = {};
uint64_t next_pending_input_sequence = 0;
std::mutex pending_input_mutex = {};

uint64_t get_monotonic_ms()
{
	using namespace std::chrono;
	return static_cast<uint64_t>(
	        duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count());
}

std::string get_current_dos_path()
{
	const auto drive = static_cast<char>('A' + DOS_GetDefaultDrive());
	char dir[DOS_PATHLENGTH] = {};
	if (!DOS_GetCurrentDir(0, dir, true) || dir[0] == 0) {
		return std::string(1, drive) + ":\\";
	}
	return std::string(1, drive) + ":\\" + dir;
}

void populate_command_result(CommandResult &result)
{
	result.errorlevel = dos.return_code;
	result.drive.assign(1, static_cast<char>('A' + DOS_GetDefaultDrive()));
	result.cwd = get_current_dos_path();
}

StatusSnapshot snapshot_status(const Options &options)
{
	StatusSnapshot status = {};
	status.transport = options.transport;
	status.session_active = session_active;
	status.errorlevel = dos.return_code;
	status.drive.assign(1, static_cast<char>('A' + DOS_GetDefaultDrive()));
	status.cwd = get_current_dos_path();
	return status;
}

bool emit_session_line(const std::string &line)
{
	if (line.empty()) {
		return true;
	}

	if (!active_write_line) {
		session_write_failed.store(true);
		return false;
	}

	if (!active_write_line(line)) {
		session_write_failed.store(true);
		return false;
	}

	return true;
}

void reset_session_state()
{
	reset_buffered_output(buffered_output, {});
	active_request_id.clear();
	active_write_line = {};
	session_write_failed.store(false);
	session_active = false;
}

#if defined(__unix__) || defined(__APPLE__)
std::string make_errno_message(const char *action, const std::string &path = {})
{
	std::string message = action;
	if (!path.empty()) {
		message += " ";
		message += path;
	}
	message += ": ";
	message += std::strerror(errno);
	return message;
}

bool write_fd_line(const int fd, const std::string &line)
{
	const char *data = line.data();
	std::size_t remaining = line.size();

	while (remaining > 0) {
		const auto written = write(fd, data, remaining);
		if (written < 0) {
			if (errno == EINTR) {
				continue;
			}
			return false;
		}

		data += written;
		remaining -= static_cast<std::size_t>(written);
	}

	return true;
}

bool read_fd_line(const int fd, std::string &line)
{
	line.clear();

	for (;;) {
		char byte = 0;
		const auto received = read(fd, &byte, 1);
		if (received == 0) {
			return !line.empty();
		}
		if (received < 0) {
			if (errno == EINTR) {
				continue;
			}
			return false;
		}

		if (byte == '\n') {
			return true;
		}
		if (byte == '\r') {
			continue;
		}

		line += byte;
	}
}

void close_fd(int &fd)
{
	if (fd >= 0) {
		(void)close(fd);
		fd = -1;
	}
}

struct SocketSessionState {
	int client_fd = -1;
	std::mutex mutex = {};
	std::condition_variable condition = {};
	std::deque<Request> requests = {};
	bool disconnected = false;
	bool had_io_error = false;
};

bool write_socket_session_line(SocketSessionState &state, const std::string &line)
{
	if (line.empty()) {
		return true;
	}

	std::lock_guard<std::mutex> lock(state.mutex);
	if (!write_fd_line(state.client_fd, line)) {
		state.had_io_error = true;
		state.disconnected = true;
		session_write_failed.store(true);
		state.condition.notify_all();
		return false;
	}

	return true;
}

void handle_socket_input_request(SocketSessionState &state, const Request &request)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};
	const bool built = request.op == "input_text"
	                         ? build_input_codes_for_text(request.text, codes, error)
	                         : build_input_codes_for_key(request.key, codes, error);
	if (!built) {
		(void)write_socket_session_line(state, make_error_json_line(request.id, error));
		return;
	}

	const auto queued = queue_input_codes(codes);
	if (!queued.ok) {
		(void)write_socket_session_line(state, make_error_json_line(request.id, queued.error));
		return;
	}

	(void)write_socket_session_line(
	        state, make_input_result_json_line(request.id, true, queued.queued));
}

void socket_session_reader(SocketSessionState &state)
{
	for (;;) {
		std::string line = {};
		if (!read_fd_line(state.client_fd, line)) {
			std::lock_guard<std::mutex> lock(state.mutex);
			state.disconnected = true;
			state.condition.notify_all();
			return;
		}
		if (line.empty()) {
			continue;
		}

		const auto request = parse_request_line(line);
		if (!request.ok) {
			(void)write_socket_session_line(
			        state, make_error_json_line(request.id, request.error));
			continue;
		}

		if (request.op == "input_text" || request.op == "key") {
			handle_socket_input_request(state, request);
			continue;
		}

		{
			std::lock_guard<std::mutex> lock(state.mutex);
			state.requests.push_back(request);
		}
		state.condition.notify_all();
	}
}

bool is_socket_session_disconnected(SocketSessionState &state)
{
	std::lock_guard<std::mutex> lock(state.mutex);
	return state.disconnected;
}

bool mark_socket_session_disconnected(SocketSessionState &state, const bool had_io_error)
{
	std::lock_guard<std::mutex> lock(state.mutex);
	state.disconnected = true;
	state.had_io_error = state.had_io_error || had_io_error;
	state.condition.notify_all();
	return true;
}

bool is_socket_would_block_error(const int error)
{
#if EAGAIN == EWOULDBLOCK
	return error == EAGAIN;
#else
	return error == EAGAIN || error == EWOULDBLOCK;
#endif
}

bool has_socket_session_peer_disconnected(SocketSessionState &state)
{
	char byte = 0;
	const auto received = recv(state.client_fd, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
	if (received == 0) {
		return mark_socket_session_disconnected(state, false);
	}
	if (received > 0) {
		return false;
	}
	if (is_socket_would_block_error(errno) || errno == EINTR) {
		return is_socket_session_disconnected(state);
	}

	return mark_socket_session_disconnected(state, true);
}

struct PipeSessionState {
	int read_fd = -1;
	int write_fd = -1;
	std::mutex mutex = {};
	std::condition_variable condition = {};
	std::deque<Request> requests = {};
	bool disconnected = false;
	bool stop_reader = false;
	bool had_io_error = false;
};

bool write_pipe_session_line(PipeSessionState &state, const std::string &line)
{
	if (line.empty()) {
		return true;
	}

	std::lock_guard<std::mutex> lock(state.mutex);
	if (!write_fd_line(state.write_fd, line)) {
		state.had_io_error = true;
		state.disconnected = true;
		session_write_failed.store(true);
		state.condition.notify_all();
		return false;
	}

	return true;
}

bool should_stop_pipe_reader(PipeSessionState &state)
{
	std::lock_guard<std::mutex> lock(state.mutex);
	return state.stop_reader;
}

bool read_pipe_line(PipeSessionState &state, std::string &line)
{
	line.clear();

	for (;;) {
		if (should_stop_pipe_reader(state)) {
			return false;
		}

		fd_set read_fds;
		FD_ZERO(&read_fds);
		FD_SET(state.read_fd, &read_fds);
		timeval timeout = {};
		timeout.tv_usec = 100000;
		const auto selected = select(state.read_fd + 1, &read_fds, nullptr, nullptr, &timeout);
		if (selected < 0) {
			if (errno == EINTR) {
				continue;
			}
			return false;
		}
		if (selected == 0) {
			continue;
		}

		char byte = 0;
		const auto received = read(state.read_fd, &byte, 1);
		if (received == 0) {
			return !line.empty();
		}
		if (received < 0) {
			if (errno == EINTR || is_socket_would_block_error(errno)) {
				continue;
			}
			return false;
		}

		if (byte == '\n') {
			return true;
		}
		if (byte == '\r') {
			continue;
		}

		line += byte;
	}
}

void handle_pipe_input_request(PipeSessionState &state, const Request &request)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};
	const bool built = request.op == "input_text"
	                         ? build_input_codes_for_text(request.text, codes, error)
	                         : build_input_codes_for_key(request.key, codes, error);
	if (!built) {
		(void)write_pipe_session_line(state, make_error_json_line(request.id, error));
		return;
	}

	const auto queued = queue_input_codes(codes);
	if (!queued.ok) {
		(void)write_pipe_session_line(state, make_error_json_line(request.id, queued.error));
		return;
	}

	(void)write_pipe_session_line(
	        state, make_input_result_json_line(request.id, true, queued.queued));
}

void pipe_session_reader(PipeSessionState &state)
{
	for (;;) {
		std::string line = {};
		if (!read_pipe_line(state, line)) {
			std::lock_guard<std::mutex> lock(state.mutex);
			state.disconnected = true;
			state.condition.notify_all();
			return;
		}
		if (line.empty()) {
			continue;
		}

		const auto request = parse_request_line(line);
		if (!request.ok) {
			(void)write_pipe_session_line(
			        state, make_error_json_line(request.id, request.error));
			continue;
		}

		if (request.op == "input_text" || request.op == "key") {
			handle_pipe_input_request(state, request);
			continue;
		}

		{
			std::lock_guard<std::mutex> lock(state.mutex);
			state.requests.push_back(request);
		}
		state.condition.notify_all();
	}
}

bool is_pipe_session_disconnected(PipeSessionState &state)
{
	std::lock_guard<std::mutex> lock(state.mutex);
	return state.disconnected;
}

void stop_pipe_session_reader(PipeSessionState &state)
{
	std::lock_guard<std::mutex> lock(state.mutex);
	state.stop_reader = true;
	state.disconnected = true;
	state.condition.notify_all();
}
#else
bool read_stdin_line(std::string &line)
{
	line.clear();

	char buffer[4096] = {};
	if (std::fgets(buffer, sizeof(buffer), stdin) == nullptr) {
		return false;
	}

	line.assign(buffer);
	while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) {
		line.pop_back();
	}

	return true;
}

bool write_stdout_line(const std::string &line)
{
	if (line.empty()) {
		return true;
	}

	return std::fwrite(line.data(), 1, line.size(), stdout) == line.size() &&
	       std::fflush(stdout) == 0;
}
#endif

} // namespace

SessionResult run_control_session(const Options &options,
                                  const ReadLineFn &read_line,
                                  const WriteLineFn &write_line,
                                  const ExecRequestFn &exec_request)
{
	SessionResult result = {};
	active_write_line = write_line;
	active_request_id.clear();
	reset_buffered_output(buffered_output, {});
	session_write_failed.store(false);
	session_active = true;

	if (!emit_session_line(make_ready_json_line(options))) {
		result.had_io_error = true;
		reset_session_state();
		return result;
	}

	result.started = true;

	for (;;) {
		std::string line = {};
		if (!read_line(line)) {
			break;
		}
		if (line.empty()) {
			continue;
		}

		const auto request = parse_request_line(line);
		if (!request.ok) {
			if (!emit_session_line(make_error_json_line(request.id, request.error))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		if (request.op == "status") {
			if (!emit_session_line(make_status_json_line(request.id, snapshot_status(options)))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		if (request.op == "input_text" || request.op == "key") {
			std::vector<uint16_t> codes = {};
			std::string error = {};
			const bool built = request.op == "input_text"
			                         ? build_input_codes_for_text(request.text, codes, error)
			                         : build_input_codes_for_key(request.key, codes, error);
			if (!built) {
				if (!emit_session_line(make_error_json_line(request.id, error))) {
					result.had_io_error = true;
					break;
				}
				continue;
			}

			const auto queued = queue_input_codes(codes);
			if (!queued.ok) {
				if (!emit_session_line(make_error_json_line(request.id, queued.error))) {
					result.had_io_error = true;
					break;
				}
				continue;
			}

			if (!emit_session_line(make_input_result_json_line(request.id, true, queued.queued))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		if (request.op != "exec") {
			if (!emit_session_line(make_error_json_line(request.id, "unsupported op"))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		CommandResult command_result = {};
		active_request_id = request.id;
		reset_buffered_output(buffered_output, request.id);
		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		const auto end_ms = get_monotonic_ms();
		command_result.duration_ms = end_ms >= start_ms ? (end_ms - start_ms) : 0;
		if (session_write_failed.load() ||
		    !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
			result.had_io_error = true;
			active_request_id.clear();
			break;
		}
		active_request_id.clear();

		if (!emit_session_line(make_exec_result_json_line(request.id, ok, command_result))) {
			result.had_io_error = true;
			break;
		}
		if (command_result.shell_exit) {
			break;
		}
	}

	if (!result.had_io_error && !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
		result.had_io_error = true;
	}

	reset_session_state();
	return result;
}

SessionResult run_control_socket_session(const Options &options,
                                         int client_fd,
                                         const ExecRequestFn &exec_request)
{
	SessionResult result = {};

#if !defined(__unix__) && !defined(__APPLE__)
	(void)options;
	(void)client_fd;
	(void)exec_request;
	result.had_io_error = true;
	return result;
#else
	SocketSessionState state = {};
	state.client_fd = client_fd;

	active_write_line = [&state](const std::string &line) {
		return write_socket_session_line(state, line);
	};
	active_request_id.clear();
	reset_buffered_output(buffered_output, {});
	session_write_failed.store(false);
	session_active = true;

	if (!emit_session_line(make_ready_json_line(options))) {
		result.had_io_error = true;
		close_fd(state.client_fd);
		reset_session_state();
		return result;
	}

	result.started = true;
	std::thread reader(socket_session_reader, std::ref(state));

	for (;;) {
		Request request = {};
		{
			std::unique_lock<std::mutex> lock(state.mutex);
			state.condition.wait(lock, [&state]() {
				return !state.requests.empty() || state.disconnected;
			});
			if (state.disconnected) {
				break;
			}
			if (!state.requests.empty()) {
				request = state.requests.front();
				state.requests.pop_front();
			}
		}

		if (has_socket_session_peer_disconnected(state)) {
			break;
		}

		if (request.op == "status") {
			if (!emit_session_line(make_status_json_line(request.id, snapshot_status(options)))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		if (request.op != "exec") {
			if (!emit_session_line(make_error_json_line(request.id, "unsupported op"))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		CommandResult command_result = {};
		active_request_id = request.id;
		reset_buffered_output(buffered_output, request.id);
		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		const auto end_ms = get_monotonic_ms();
		command_result.duration_ms = end_ms >= start_ms ? (end_ms - start_ms) : 0;
		if (has_socket_session_peer_disconnected(state)) {
			active_request_id.clear();
			break;
		}
		if (session_write_failed.load() ||
		    !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
			result.had_io_error = true;
			active_request_id.clear();
			break;
		}
		active_request_id.clear();

		if (!emit_session_line(make_exec_result_json_line(request.id, ok, command_result))) {
			result.had_io_error = true;
			break;
		}
		if (command_result.shell_exit) {
			break;
		}
	}

	if (!result.had_io_error && !has_socket_session_peer_disconnected(state) &&
	    !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
		result.had_io_error = true;
	}

	(void)shutdown(state.client_fd, SHUT_RDWR);
	if (reader.joinable()) {
		reader.join();
	}

	{
		std::lock_guard<std::mutex> lock(state.mutex);
		result.had_io_error = result.had_io_error || state.had_io_error;
	}

	close_fd(state.client_fd);
	reset_session_state();
	return result;
#endif
}

SessionResult run_control_pipe_session(const Options &options,
                                       int read_fd,
                                       int write_fd,
                                       const ExecRequestFn &exec_request)
{
	SessionResult result = {};

#if !defined(__unix__) && !defined(__APPLE__)
	(void)options;
	(void)read_fd;
	(void)write_fd;
	(void)exec_request;
	result.had_io_error = true;
	return result;
#else
	PipeSessionState state = {};
	state.read_fd = read_fd;
	state.write_fd = write_fd;

	active_write_line = [&state](const std::string &line) {
		return write_pipe_session_line(state, line);
	};
	active_request_id.clear();
	reset_buffered_output(buffered_output, {});
	session_write_failed.store(false);
	session_active = true;

	if (!emit_session_line(make_ready_json_line(options))) {
		result.had_io_error = true;
		close_fd(state.read_fd);
		close_fd(state.write_fd);
		reset_session_state();
		return result;
	}

	result.started = true;
	std::thread reader(pipe_session_reader, std::ref(state));

	for (;;) {
		Request request = {};
		{
			std::unique_lock<std::mutex> lock(state.mutex);
			state.condition.wait(lock, [&state]() {
				return !state.requests.empty() || state.disconnected;
			});
			if (state.disconnected && state.requests.empty()) {
				break;
			}
			if (!state.requests.empty()) {
				request = state.requests.front();
				state.requests.pop_front();
			}
		}

		if (is_pipe_session_disconnected(state)) {
			break;
		}

		if (request.op == "status") {
			if (!emit_session_line(make_status_json_line(request.id, snapshot_status(options)))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		if (request.op != "exec") {
			if (!emit_session_line(make_error_json_line(request.id, "unsupported op"))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		CommandResult command_result = {};
		active_request_id = request.id;
		reset_buffered_output(buffered_output, request.id);
		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		const auto end_ms = get_monotonic_ms();
		command_result.duration_ms = end_ms >= start_ms ? (end_ms - start_ms) : 0;
		if (is_pipe_session_disconnected(state)) {
			active_request_id.clear();
			break;
		}
		if (session_write_failed.load() ||
		    !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
			result.had_io_error = true;
			active_request_id.clear();
			break;
		}
		active_request_id.clear();

		if (!emit_session_line(make_exec_result_json_line(request.id, ok, command_result))) {
			result.had_io_error = true;
			break;
		}
		if (command_result.shell_exit) {
			break;
		}
	}

	if (!result.had_io_error && !is_pipe_session_disconnected(state) &&
	    !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
		result.had_io_error = true;
	}

	stop_pipe_session_reader(state);
	if (reader.joinable()) {
		reader.join();
	}

	{
		std::lock_guard<std::mutex> lock(state.mutex);
		result.had_io_error = result.had_io_error || state.had_io_error;
	}

	close_fd(state.read_fd);
	close_fd(state.write_fd);
	reset_session_state();
	return result;
#endif
}

bool run_stdio_shell()
{
	if (control == nullptr || !is_stdio_enabled(control->opt_host_control)) {
		return false;
	}

#if defined(__unix__) || defined(__APPLE__)
	int stdin_fd = dup(STDIN_FILENO);
	int stdout_fd = dup(STDOUT_FILENO);
	if (stdin_fd < 0 || stdout_fd < 0) {
		std::fprintf(stderr, "%s\n", std::strerror(errno));
		std::fflush(stderr);
		close_fd(stdin_fd);
		close_fd(stdout_fd);
		return false;
	}

	const auto result = run_control_session(
	        control->opt_host_control,
	        [stdin_fd](std::string &line) { return read_fd_line(stdin_fd, line); },
	        [stdout_fd](const std::string &line) { return write_fd_line(stdout_fd, line); },
	        [](const Request &request, CommandResult &result) {
		        const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
		        if (ok) {
			        populate_command_result(result);
		        }
		        return ok;
	        });

	close_fd(stdin_fd);
	close_fd(stdout_fd);
	return result.started;
#else
	const auto result = run_control_session(
	        control->opt_host_control,
	        read_stdin_line,
	        write_stdout_line,
	        [](const Request &request, CommandResult &result) {
		        const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
		        if (ok) {
			        populate_command_result(result);
		        }
		        return ok;
	        });
	return result.started;
#endif
}

bool run_pipe_shell()
{
	if (control == nullptr || !is_pipe_enabled(control->opt_host_control)) {
		return false;
	}

#if !defined(__unix__) && !defined(__APPLE__)
	std::fprintf(stderr, "Host control pipe transport is unsupported on this platform\n");
	std::fflush(stderr);
	return false;
#else
	PipeServer server = {};
	std::string error = {};
	if (!open_pipe_server(control->opt_host_control.endpoint, server, error)) {
		std::fprintf(stderr, "%s\n", error.c_str());
		std::fflush(stderr);
		return false;
	}

	int output_fd = -1;
	do {
		output_fd = open(server.output_path.c_str(), O_WRONLY);
	} while (output_fd < 0 && errno == EINTR);

	if (output_fd < 0) {
		error = make_errno_message("failed to open host control pipe output",
		                           server.output_path);
		close_pipe_server(server);
		std::fprintf(stderr, "%s\n", error.c_str());
		std::fflush(stderr);
		return false;
	}

	const int input_fd = server.input_fd;
	server.input_fd = -1;
	const auto result = run_control_pipe_session(
	        control->opt_host_control,
	        input_fd,
	        output_fd,
	        [](const Request &request, CommandResult &result) {
		        const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
		        if (ok) {
			        populate_command_result(result);
		        }
		        return ok;
	        });

	close_pipe_server(server);
	return result.started;
#endif
}

bool open_pipe_server(const std::string &path, PipeServer &server, std::string &error)
{
	server = {};
	error.clear();

#if !defined(__unix__) && !defined(__APPLE__)
	error = "host control pipe transport is unsupported on this platform";
	return false;
#else
	if (path.empty()) {
		error = "host control pipe path is empty";
		return false;
	}

	server.base_path = path;
	server.input_path = path + ".in";
	server.output_path = path + ".out";

	if (unlink(server.input_path.c_str()) != 0 && errno != ENOENT) {
		error = make_errno_message("failed to remove stale host control pipe",
		                           server.input_path);
		return false;
	}
	if (unlink(server.output_path.c_str()) != 0 && errno != ENOENT) {
		error = make_errno_message("failed to remove stale host control pipe",
		                           server.output_path);
		return false;
	}

	if (mkfifo(server.input_path.c_str(), 0600) != 0) {
		error = make_errno_message("failed to create host control pipe",
		                           server.input_path);
		return false;
	}
	server.created_input_path = true;

	if (mkfifo(server.output_path.c_str(), 0600) != 0) {
		error = make_errno_message("failed to create host control pipe",
		                           server.output_path);
		close_pipe_server(server);
		return false;
	}
	server.created_output_path = true;

	server.input_fd = open(server.input_path.c_str(), O_RDONLY | O_NONBLOCK);
	if (server.input_fd < 0) {
		error = make_errno_message("failed to open host control pipe input",
		                           server.input_path);
		close_pipe_server(server);
		return false;
	}

	return true;
#endif
}

void close_pipe_server(PipeServer &server)
{
#if defined(__unix__) || defined(__APPLE__)
	close_fd(server.input_fd);
	if (server.created_input_path && !server.input_path.empty()) {
		(void)unlink(server.input_path.c_str());
	}
	if (server.created_output_path && !server.output_path.empty()) {
		(void)unlink(server.output_path.c_str());
	}
#endif
	server = {};
}

bool open_socket_server(const std::string &path, SocketServer &server, std::string &error)
{
	server = {};
	error.clear();

#if !defined(__unix__) && !defined(__APPLE__)
	error = "host control socket transport is unsupported on this platform";
	return false;
#else
	if (path.empty()) {
		error = "host control socket path is empty";
		return false;
	}
	sockaddr_un addr = {};
	if (path.size() >= sizeof(addr.sun_path)) {
		error = "host control socket path is too long";
		return false;
	}

	if (unlink(path.c_str()) != 0 && errno != ENOENT) {
		error = make_errno_message("failed to remove stale host control socket", path);
		return false;
	}

	server.listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (server.listen_fd < 0) {
		error = make_errno_message("failed to create host control socket", path);
		return false;
	}

	addr.sun_family = AF_UNIX;
	std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1u);

	if (bind(server.listen_fd, reinterpret_cast<const sockaddr *>(&addr), sizeof(addr)) != 0) {
		error = make_errno_message("failed to bind host control socket", path);
		close_fd(server.listen_fd);
		return false;
	}

	server.path = path;
	server.created_path = true;

	if (listen(server.listen_fd, 1) != 0) {
		error = make_errno_message("failed to listen on host control socket", path);
		close_socket_server(server);
		return false;
	}

	return true;
#endif
}

void close_socket_server(SocketServer &server)
{
#if defined(__unix__) || defined(__APPLE__)
	close_fd(server.listen_fd);
	if (server.created_path && !server.path.empty()) {
		(void)unlink(server.path.c_str());
	}
#endif
	server = {};
}

bool run_socket_shell()
{
	if (control == nullptr || !is_socket_enabled(control->opt_host_control)) {
		return false;
	}

#if !defined(__unix__) && !defined(__APPLE__)
	std::fprintf(stderr, "Host control socket transport is unsupported on this platform\n");
	std::fflush(stderr);
	return false;
#else
	SocketServer server = {};
	std::string error = {};
	if (!open_socket_server(control->opt_host_control.endpoint, server, error)) {
		std::fprintf(stderr, "%s\n", error.c_str());
		std::fflush(stderr);
		return false;
	}

	int client_fd = -1;
	do {
		client_fd = accept(server.listen_fd, nullptr, nullptr);
	} while (client_fd < 0 && errno == EINTR);

	if (client_fd < 0) {
		error = make_errno_message("failed to accept host control socket client",
		                           control->opt_host_control.endpoint);
		close_socket_server(server);
		std::fprintf(stderr, "%s\n", error.c_str());
		std::fflush(stderr);
		return false;
	}

	const auto result = run_control_socket_session(
	        control->opt_host_control,
	        client_fd,
	        [](const Request &request, CommandResult &result) {
		        const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
		        if (ok) {
			        populate_command_result(result);
		        }
		        return ok;
	        });

	close_socket_server(server);
	return result.started;
#endif
}

bool build_input_codes_for_text(const std::string &text,
                                std::vector<uint16_t> &codes,
                                std::string &error)
{
	codes.clear();
	error.clear();

	for (const auto ch : text) {
		const auto byte = static_cast<unsigned char>(ch);
		if (byte == '\r' || byte == '\n') {
			codes.push_back(0x1c0d);
			continue;
		}
		if (byte < 0x20 || byte > 0x7e) {
			error = "input_text supports ASCII only";
			codes.clear();
			return false;
		}
		codes.push_back(static_cast<uint16_t>(byte));
	}

	return true;
}

bool build_input_codes_for_key(const std::string &key,
                               std::vector<uint16_t> &codes,
                               std::string &error)
{
	static const std::map<std::string, uint16_t> named_keys = {
	        {"enter", 0x1c0d},
	        {"escape", 0x011b},
	        {"tab", 0x0f09},
	        {"backspace", 0x0e08},
	        {"up", 0x4800},
	        {"down", 0x5000},
	        {"left", 0x4b00},
	        {"right", 0x4d00},
	};

	codes.clear();
	error.clear();

	const auto it = named_keys.find(key);
	if (it == named_keys.end()) {
		error = "unsupported key";
		return false;
	}

	codes.push_back(it->second);
	return true;
}

InputQueueResult queue_input_codes(const std::vector<uint16_t> &codes)
{
	InputQueueResult result = {};
	std::lock_guard<std::mutex> lock(pending_input_mutex);

	if (pending_input_codes.size() + codes.size() > input_queue_max_codes) {
		result.error = "input queue full";
		return result;
	}

	for (const auto code : codes) {
		pending_input_codes.push_back({next_pending_input_sequence++, code});
	}

	result.ok = true;
	result.queued = codes.size();
	return result;
}

void clear_queued_input()
{
	std::lock_guard<std::mutex> lock(pending_input_mutex);
	pending_input_codes.clear();
}

std::size_t drain_queued_input()
{
	std::size_t drained = 0;

	for (;;) {
		uint64_t sequence = 0;
		uint16_t code = 0;
		{
			std::lock_guard<std::mutex> lock(pending_input_mutex);
			if (pending_input_codes.empty()) {
				return drained;
			}
			sequence = pending_input_codes.front().sequence;
			code = pending_input_codes.front().code;
		}

		if (!BIOS_AddKeyToBuffer(code)) {
			return drained;
		}

		{
			std::lock_guard<std::mutex> lock(pending_input_mutex);
			if (!pending_input_codes.empty() && pending_input_codes.front().sequence == sequence) {
				pending_input_codes.pop_front();
			}
		}
		++drained;
	}
}

std::size_t drain_queued_input_codes_for_test(std::vector<uint16_t> &codes, const std::size_t max_codes)
{
	std::lock_guard<std::mutex> lock(pending_input_mutex);
	std::size_t drained = 0;
	while (drained < max_codes && !pending_input_codes.empty()) {
		codes.push_back(pending_input_codes.front().code);
		pending_input_codes.pop_front();
		++drained;
	}
	return drained;
}

void capture_dos_write(const uint16_t info, const char *name, const uint8_t *data, const std::size_t size)
{
	if (!session_active || active_request_id.empty() || data == nullptr || size == 0) {
		return;
	}

	if (!should_capture_dos_write(info, name)) {
		return;
	}

	const auto now_ms = get_monotonic_ms();
	append_buffered_output(buffered_output, data, size, now_ms);
	if (should_flush_buffered_output(
	            buffered_output, now_ms, buffered_output_max_bytes, buffered_output_max_ms)) {
		(void)emit_session_line(flush_buffered_output_json_line(buffered_output));
	}
}

} // namespace host_control
