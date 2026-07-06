#include "host_control.h"

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>

#if defined(__unix__) || defined(__APPLE__)
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#endif

#include "control.h"
#include "dos_inc.h"
#include "shell.h"

namespace host_control {
namespace {

constexpr std::size_t buffered_output_max_bytes = 4096;
constexpr uint64_t buffered_output_max_ms = 100;

bool session_active = false;
bool session_write_failed = false;
std::string active_request_id = {};
BufferedOutput buffered_output = {};
WriteLineFn active_write_line = {};

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
		session_write_failed = true;
		return false;
	}

	if (!active_write_line(line)) {
		session_write_failed = true;
		return false;
	}

	return true;
}

void reset_session_state()
{
	reset_buffered_output(buffered_output, {});
	active_request_id.clear();
	active_write_line = {};
	session_write_failed = false;
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
	session_write_failed = false;
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

		CommandResult command_result = {};
		active_request_id = request.id;
		reset_buffered_output(buffered_output, request.id);
		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		const auto end_ms = get_monotonic_ms();
		command_result.duration_ms = end_ms >= start_ms ? (end_ms - start_ms) : 0;
		if (session_write_failed ||
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

	std::fprintf(stderr, "Host control pipe transport is not implemented\n");
	std::fflush(stderr);
	return false;
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

	const auto result = run_control_session(
	        control->opt_host_control,
	        [client_fd](std::string &line) { return read_fd_line(client_fd, line); },
	        [client_fd](const std::string &line) { return write_fd_line(client_fd, line); },
	        [](const Request &request, CommandResult &result) {
		        const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
		        if (ok) {
			        populate_command_result(result);
		        }
		        return ok;
	        });

	close_fd(client_fd);
	close_socket_server(server);
	return result.started;
#endif
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
