#include "host_control.h"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <limits>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#if defined(_WIN32) || defined(WIN32)
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
#endif

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

extern bool ctrlbrk;

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
std::atomic<bool> exec_in_progress = {false};
std::atomic<bool> cancel_requested = {false};
bool console_device_capture_active = false;
uint16_t max_errorlevel_seen = 0;

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
	result.max_errorlevel = take_max_errorlevel();
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
	exec_in_progress.store(false);
}

bool stage_exec_input(const Request &request, std::string &error)
{
	if (request.input.empty()) {
		return true;
	}

	std::vector<uint16_t> codes = {};
	if (!build_input_codes_for_text(request.input, codes, error)) {
		return false;
	}

	const auto queued = queue_input_codes(codes);
	if (!queued.ok) {
		error = queued.error;
		return false;
	}

	return true;
}

bool request_cancel()
{
	if (!exec_in_progress.load()) {
		return false;
	}

	ctrlbrk = true;
	cancel_requested.store(true);
	std::vector<uint16_t> codes = {};
	std::string error = {};
	if (build_input_codes_for_text(std::string(1, '\x03'), codes, error)) {
		(void)queue_input_codes(codes);
	}
	return true;
}

struct DuplexSessionState {
	std::mutex mutex = {};
	std::condition_variable condition = {};
	std::deque<Request> requests = {};
	bool disconnected = false;
	bool had_io_error = false;
	WriteLineFn write_line = {};
};

bool write_duplex_session_line(DuplexSessionState &state, const std::string &line)
{
	if (line.empty()) {
		return true;
	}

	std::lock_guard<std::mutex> lock(state.mutex);
	if (!state.write_line(line)) {
		state.had_io_error = true;
		state.disconnected = true;
		session_write_failed.store(true);
		state.condition.notify_all();
		return false;
	}

	return true;
}

void handle_duplex_input_request(DuplexSessionState &state, const Request &request)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};
	const bool built = request.op == "input_text"
	                         ? build_input_codes_for_text(request.text, codes, error)
	                         : build_input_codes_for_key(request.key, codes, error);
	if (!built) {
		(void)write_duplex_session_line(state, make_error_json_line(request.id, error));
		return;
	}

	const auto queued = queue_input_codes(codes);
	if (!queued.ok) {
		(void)write_duplex_session_line(state, make_error_json_line(request.id, queued.error));
		return;
	}

	(void)write_duplex_session_line(
	        state, make_input_result_json_line(request.id, true, queued.queued));
}

void duplex_session_reader(DuplexSessionState &state, const ReadLineFn &read_line)
{
	for (;;) {
		std::string line = {};
		if (!read_line(line)) {
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
			(void)write_duplex_session_line(
			        state, make_error_json_line(request.id, request.error));
			continue;
		}

		if (request.op == "cancel" || request.op == "break") {
			const bool cancelled = request_cancel();
			if (cancelled) {
				(void)write_duplex_session_line(state, make_cancel_result_json_line(request.id, true));
			} else {
				(void)write_duplex_session_line(state, make_error_json_line(request.id, "no command is running"));
			}
			continue;
		}

		if (request.op == "input_text" || request.op == "key") {
			handle_duplex_input_request(state, request);
			continue;
		}

		{
			std::lock_guard<std::mutex> lock(state.mutex);
			state.requests.push_back(request);
		}
		state.condition.notify_all();
	}
}

bool is_duplex_session_disconnected(DuplexSessionState &state,
	                                 const PeerDisconnectedFn &peer_disconnected)
{
	{
		std::lock_guard<std::mutex> lock(state.mutex);
		if (state.disconnected) {
			return true;
		}
	}
	return peer_disconnected && peer_disconnected();
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

		if (request.op == "cancel" || request.op == "break") {
			const bool cancelled = request_cancel();
			if (cancelled) {
				(void)write_socket_session_line(state, make_cancel_result_json_line(request.id, true));
			} else {
				(void)write_socket_session_line(state, make_error_json_line(request.id, "no command is running"));
			}
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
	bool disconnected = false;
	bool stop_reader = false;
};

bool write_pipe_session_line(PipeSessionState &state, const std::string &line)
{
	if (line.empty()) {
		return true;
	}

	return write_fd_line(state.write_fd, line);
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

static bool ascii_case_insensitive_prefix(const std::string &value, const std::string &prefix)
{
	if (value.size() < prefix.size()) {
		return false;
	}

	for (std::size_t i = 0; i < prefix.size(); ++i) {
		const auto lower = [](const char c) {
			return (c >= 'A' && c <= 'Z') ? static_cast<char>(c + ('a' - 'A')) : c;
		};
		if (lower(value[i]) != lower(prefix[i])) {
			return false;
		}
	}

	return true;
}

std::string normalize_windows_pipe_endpoint(const std::string &value,
                                            std::string &error)
{
	const std::string prefix = "\\\\.\\pipe\\";
	error.clear();
	if (value.empty() || value.find('\0') != std::string::npos) {
		error = "Windows pipe endpoint must not be empty or contain NUL";
		return {};
	}

	std::string normalized = value;
	if (ascii_case_insensitive_prefix(value, prefix)) {
		normalized = prefix + value.substr(prefix.size());
	} else {
		if (value.compare(0, 2, "\\\\") == 0 ||
		    value.find('\\') != std::string::npos || value.find('/') != std::string::npos) {
			error = "Windows pipe endpoint must be a short name or local \\\\.\\pipe\\ path";
			return {};
		}
		normalized = prefix + value;
	}

	if (normalized.size() == prefix.size() || normalized.size() > 256) {
		error = "Windows pipe endpoint name is empty or exceeds 256 characters";
		return {};
	}

	return normalized;
}

bool append_line_bytes(LineAccumulator &state,
                       const char *data,
                       const std::size_t size,
                       const std::size_t max_line_bytes)
{
	for (std::size_t i = 0; i < size; ++i) {
		const char byte = data[i];
		if (byte == '\n') {
			if (!state.buffered.empty() && state.buffered.back() == '\r')
				state.buffered.pop_back();
			state.lines.push_back(state.buffered);
			state.buffered.clear();
			continue;
		}
		if (state.buffered.size() == max_line_bytes) {
			state.failed = true;
			state.error = "request line exceeds 1048576 bytes";
			return false;
		}
		state.buffered.push_back(byte);
	}
	return true;
}

#if defined(_WIN32) || defined(WIN32)
namespace {

std::string make_win32_message(const char *action, const std::string &endpoint,
                               const DWORD code);

HANDLE pipe_server_handle(const PipeServer &server)
{
	return reinterpret_cast<HANDLE>(server.native_handle);
}

class SecurityDescriptorHolder {
public:
	~SecurityDescriptorHolder()
	{
		reset();
	}

	SecurityDescriptorHolder() = default;

	SecurityDescriptorHolder(const SecurityDescriptorHolder &) = delete;
	SecurityDescriptorHolder &operator=(const SecurityDescriptorHolder &) = delete;

	void reset(PSECURITY_DESCRIPTOR descriptor = nullptr)
	{
		if (descriptor_ != nullptr) {
			(void)LocalFree(descriptor_);
		}
		descriptor_ = descriptor;
	}

	PSECURITY_DESCRIPTOR get() const
	{
		return descriptor_;
	}

private:
	PSECURITY_DESCRIPTOR descriptor_ = nullptr;
};

bool build_windows_pipe_security_descriptor(const std::string &endpoint,
                                            std::string &error,
                                            PSECURITY_DESCRIPTOR &descriptor)
{
	descriptor = nullptr;

	HANDLE token = nullptr;
	if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token) == FALSE) {
		error = make_win32_message("failed to open process token for host control pipe",
		                           endpoint,
		                           GetLastError());
		return false;
	}

	DWORD size = 0;
	(void)GetTokenInformation(token, TokenUser, nullptr, 0, &size);
	if (size == 0) {
		(void)CloseHandle(token);
		error = make_win32_message("failed to size process token user for host control pipe",
		                           endpoint,
		                           GetLastError());
		return false;
	}

	std::vector<uint8_t> buffer(size);
	auto *const user = reinterpret_cast<PTOKEN_USER>(buffer.data());
	if (GetTokenInformation(token, TokenUser, user, size, &size) == FALSE) {
		(void)CloseHandle(token);
		error = make_win32_message("failed to read process token user for host control pipe",
		                           endpoint,
		                           GetLastError());
		return false;
	}
	(void)CloseHandle(token);

	LPWSTR sid_string = nullptr;
	if (ConvertSidToStringSidW(user->User.Sid, &sid_string) == FALSE) {
		error = make_win32_message("failed to convert user SID for host control pipe",
		                           endpoint,
		                           GetLastError());
		return false;
	}

	const std::wstring sddl = L"D:P(A;;GA;;;SY)(A;;GA;;;" + std::wstring(sid_string) + L")";
	(void)LocalFree(sid_string);
	sid_string = nullptr;

	PSECURITY_DESCRIPTOR built = nullptr;
	if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
	            sddl.c_str(),
	            SDDL_REVISION_1,
	            &built,
	            nullptr) == FALSE) {
		error = make_win32_message("failed to build host control pipe security descriptor",
	                           endpoint,
	                           GetLastError());
		return false;
	}

	descriptor = built;
	return true;
}

bool utf8_to_wide(const std::string &value, std::wstring &wide)
{
	const auto length = MultiByteToWideChar(CP_UTF8,
	                                        MB_ERR_INVALID_CHARS,
	                                        value.data(),
	                                        static_cast<int>(value.size()),
	                                        nullptr,
	                                        0);
	if (length <= 0) {
		return false;
	}

	wide.resize(static_cast<std::size_t>(length));
	return MultiByteToWideChar(CP_UTF8,
	                           MB_ERR_INVALID_CHARS,
	                           value.data(),
	                           static_cast<int>(value.size()),
	                           &wide[0],
	                           length) == length;
}

std::string wide_to_utf8(const std::wstring &value)
{
	if (value.empty()) {
		return {};
	}

	const auto length = WideCharToMultiByte(CP_UTF8,
	                                        0,
	                                        value.data(),
	                                        static_cast<int>(value.size()),
	                                        nullptr,
	                                        0,
	                                        nullptr,
	                                        nullptr);
	if (length <= 0) {
		return "unavailable Win32 error text";
	}

	std::string utf8(static_cast<std::size_t>(length), '\0');
	if (WideCharToMultiByte(CP_UTF8,
	                        0,
	                        value.data(),
	                        static_cast<int>(value.size()),
	                        &utf8[0],
	                        length,
	                        nullptr,
	                        nullptr) != length) {
		return "unavailable Win32 error text";
	}
	return utf8;
}

std::string make_win32_message(const char *action, const std::string &endpoint, const DWORD code)
{
	LPWSTR buffer = nullptr;
	const auto length = FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER |
	                                  FORMAT_MESSAGE_FROM_SYSTEM |
	                                  FORMAT_MESSAGE_IGNORE_INSERTS,
	                                  nullptr,
	                                  code,
	                                  0,
	                                  reinterpret_cast<LPWSTR>(&buffer),
	                                  0,
	                                  nullptr);
	std::string detail = length > 0 && buffer != nullptr
	                             ? wide_to_utf8(std::wstring(buffer, length))
	                             : "unavailable Win32 error text";
	if (buffer != nullptr) {
		(void)LocalFree(buffer);
	}
	const auto last = detail.find_last_not_of(" \t\r\n");
	if (last != std::string::npos) {
		detail.erase(last + 1);
	}

	std::string message = action;
	message += " ";
	message += endpoint;
	message += ": ";
	message += detail;
	message += " (Win32 error ";
	message += std::to_string(code);
	message += ")";
	return message;
}

bool is_windows_pipe_disconnect(const DWORD error)
{
	// Windows reports a closed client with any of these depending on whether
	// the client closed its handle, exited, or was killed mid-transfer.
	return error == ERROR_BROKEN_PIPE || error == ERROR_NO_DATA ||
	       error == ERROR_PIPE_NOT_CONNECTED;
}

bool wait_for_windows_pipe_io(HANDLE handle,
                              OVERLAPPED &overlapped,
                              DWORD &transferred,
                              DWORD &error)
{
	if (WaitForSingleObject(overlapped.hEvent, INFINITE) != WAIT_OBJECT_0) {
		error = GetLastError();
		if (error == ERROR_SUCCESS) {
			error = ERROR_GEN_FAILURE;
		}
		return false;
	}
	if (GetOverlappedResult(handle, &overlapped, &transferred, FALSE) != FALSE) {
		return true;
	}
	error = GetLastError();
	return false;
}

} // namespace

bool connect_pipe_server(PipeServer &server, std::string &error)
{
	const auto handle = pipe_server_handle(server);
	OVERLAPPED overlapped = {};
	server.connect_event = reinterpret_cast<std::uintptr_t>(
	        CreateEventW(nullptr, TRUE, FALSE, nullptr));
	if (server.connect_event == 0) {
		error = make_win32_message("failed to create host control pipe connection event",
		                           server.base_path,
		                           GetLastError());
		return false;
	}
	overlapped.hEvent = reinterpret_cast<HANDLE>(server.connect_event);

	DWORD ignored = 0;
	bool connected = ConnectNamedPipe(handle, &overlapped) != FALSE;
	DWORD connect_error = connected ? ERROR_SUCCESS : GetLastError();
	if (!connected && connect_error == ERROR_IO_PENDING) {
		for (;;) {
			const DWORD wait = WaitForSingleObject(overlapped.hEvent, 100);
			if (wait == WAIT_OBJECT_0) {
				break;
			}
			if (wait != WAIT_TIMEOUT || server.stop_requested.load()) {
				const DWORD abort_error = (wait == WAIT_FAILED)
				                                  ? GetLastError()
				                                  : ERROR_OPERATION_ABORTED;
				(void)CancelIoEx(handle, &overlapped);
				connect_error = (abort_error == ERROR_SUCCESS) ? ERROR_GEN_FAILURE
				                                                 : abort_error;
				(void)CloseHandle(overlapped.hEvent);
				server.connect_event = 0;
				error = make_win32_message("failed to connect host control pipe client",
				                           server.base_path,
				                           connect_error);
				return false;
			}
		}
		connected = GetOverlappedResult(handle, &overlapped, &ignored, FALSE) != FALSE;
		connect_error = connected ? ERROR_SUCCESS : GetLastError();
	}
	(void)CloseHandle(overlapped.hEvent);
	server.connect_event = 0;
	if (connected || connect_error == ERROR_PIPE_CONNECTED) {
		server.connected = true;
		return true;
	}

	error = make_win32_message("failed to connect host control pipe client",
	                           server.base_path,
	                           connect_error);
	return false;
}

void request_pipe_server_stop(PipeServer &server)
{
	server.stop_requested.store(true);
	if (server.native_handle != 0) {
		(void)CancelIoEx(pipe_server_handle(server), nullptr);
	}
}

namespace {

struct WindowsPipeSessionState {
	HANDLE handle = INVALID_HANDLE_VALUE;
	std::string endpoint = {};
	std::atomic<bool> disconnected = {false};
	std::atomic<bool> failed = {false};
	std::mutex error_mutex = {};
	std::string error = {};
	HANDLE read_event = nullptr;
	HANDLE write_event = nullptr;
	LineAccumulator accumulator = {};
};

void set_windows_pipe_error(WindowsPipeSessionState &state,
	                         const char *action,
	                         const DWORD error)
{
	state.failed.store(true);
	state.disconnected.store(true);
	{
		std::lock_guard<std::mutex> lock(state.error_mutex);
		state.error = make_win32_message(action, state.endpoint, error);
	}
}

bool open_windows_pipe_session(WindowsPipeSessionState &state)
{
	state.read_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
	if (state.read_event == nullptr) {
		set_windows_pipe_error(state,
		                       "failed to create host control pipe read event",
		                       GetLastError());
		return false;
	}
	state.write_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
	if (state.write_event == nullptr) {
		set_windows_pipe_error(state,
		                       "failed to create host control pipe write event",
		                       GetLastError());
		return false;
	}
	return true;
}

void close_windows_pipe_session(WindowsPipeSessionState &state)
{
	if (state.read_event != nullptr) {
		(void)CloseHandle(state.read_event);
		state.read_event = nullptr;
	}
	if (state.write_event != nullptr) {
		(void)CloseHandle(state.write_event);
		state.write_event = nullptr;
	}
}

bool read_windows_pipe_line(WindowsPipeSessionState &state, std::string &line)
{
	for (;;) {
		if (!state.accumulator.lines.empty()) {
			line = std::move(state.accumulator.lines.front());
			state.accumulator.lines.pop_front();
			return true;
		}
		if (state.accumulator.failed || state.disconnected.load()) {
			return false;
		}

		char chunk[8192] = {};
		DWORD received = 0;
		OVERLAPPED overlapped = {};
		overlapped.hEvent = state.read_event;
		(void)ResetEvent(state.read_event);
		bool read = ReadFile(state.handle, chunk, sizeof(chunk), &received, &overlapped) != FALSE;
		DWORD read_error = read ? ERROR_SUCCESS : GetLastError();
		if (!read && read_error == ERROR_IO_PENDING) {
			read = wait_for_windows_pipe_io(state.handle, overlapped, received, read_error);
		}
		if (!read) {
			if (state.disconnected.load() || is_windows_pipe_disconnect(read_error)) {
				state.disconnected.store(true);
				return false;
			}
			set_windows_pipe_error(state, "failed to read host control pipe", read_error);
			return false;
		}
		if (received == 0) {
			state.disconnected.store(true);
			return false;
		}
		if (!append_line_bytes(state.accumulator, chunk, received)) {
			state.failed.store(true);
			state.disconnected.store(true);
			{
				std::lock_guard<std::mutex> lock(state.error_mutex);
				state.error = state.accumulator.error;
			}
			return false;
		}
	}
}

bool write_windows_pipe_line(WindowsPipeSessionState &state, const std::string &line)
{
	const char *data = line.data();
	std::size_t remaining = line.size();
	while (remaining > 0) {
		if (state.disconnected.load()) {
			return false;
		}

		DWORD written = 0;
		const auto max_write = (std::numeric_limits<DWORD>::max)();
		const auto requested = remaining > static_cast<std::size_t>(max_write)
		                       ? max_write
		                       : static_cast<DWORD>(remaining);
		OVERLAPPED overlapped = {};
		overlapped.hEvent = state.write_event;
		(void)ResetEvent(state.write_event);
		bool wrote = WriteFile(state.handle, data, requested, &written, &overlapped) != FALSE;
		DWORD write_error = wrote ? ERROR_SUCCESS : GetLastError();
		if (!wrote && write_error == ERROR_IO_PENDING) {
			wrote = wait_for_windows_pipe_io(state.handle, overlapped, written, write_error);
		}
		if (!wrote) {
			if (state.disconnected.load() || is_windows_pipe_disconnect(write_error)) {
				state.disconnected.store(true);
				return false;
			}
			set_windows_pipe_error(state, "failed to write host control pipe", write_error);
			return false;
		}
		if (written == 0) {
			set_windows_pipe_error(state, "failed to write host control pipe", ERROR_WRITE_FAULT);
			return false;
		}
		data += written;
		remaining -= static_cast<std::size_t>(written);
	}
	return true;
}

void stop_windows_pipe_reader(WindowsPipeSessionState &state)
{
	state.disconnected.store(true);
	if (state.handle != INVALID_HANDLE_VALUE) {
		(void)CancelIoEx(state.handle, nullptr);
		/* Flush the kernel pipe buffer before severing the connection so that
		 * any bytes the client has not read yet are not discarded. */
		(void)FlushFileBuffers(state.handle);
		(void)DisconnectNamedPipe(state.handle);
	}
}

std::string windows_pipe_session_error(WindowsPipeSessionState &state)
{
	std::lock_guard<std::mutex> lock(state.error_mutex);
	return state.error;
}

} // namespace
#else

bool connect_pipe_server(PipeServer &server, std::string &error)
{
	(void)server;
	error = "host control pipe connect is unsupported on this platform";
	return false;
}

void request_pipe_server_stop(PipeServer &server)
{
	server.stop_requested.store(true);
}
#endif

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

		if (request.op == "cancel" || request.op == "break") {
			const bool cancelled = request_cancel();
			if (!cancelled) {
				if (!emit_session_line(make_error_json_line(request.id, "no command is running"))) {
					result.had_io_error = true;
					break;
				}
				continue;
			}
			if (!emit_session_line(make_cancel_result_json_line(request.id, true))) {
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
		std::string stage_error = {};
		if (!stage_exec_input(request, stage_error)) {
			active_request_id.clear();
			if (!emit_session_line(make_error_json_line(request.id, stage_error))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}
		reset_max_errorlevel();
		cancel_requested.store(false);
		exec_in_progress.store(true);
		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		exec_in_progress.store(false);
		command_result.cancelled = cancel_requested.exchange(false);
		if (command_result.cancelled) {
			clear_queued_input();
		}
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

SessionResult run_control_duplex_session(const Options &options,
                                         const ReadLineFn &read_line,
                                         const WriteLineFn &write_line,
                                         const PeerDisconnectedFn &peer_disconnected,
                                         const StopReaderFn &stop_reader,
                                         const ExecRequestFn &exec_request)
{
	SessionResult result = {};
	DuplexSessionState state = {};
	state.write_line = write_line;

	active_write_line = [&state](const std::string &line) {
		return write_duplex_session_line(state, line);
	};
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
	std::thread reader(duplex_session_reader, std::ref(state), std::cref(read_line));

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

		if (is_duplex_session_disconnected(state, peer_disconnected)) {
			break;
		}
		if (request.op == "status") {
			if (!emit_session_line(make_status_json_line(request.id, snapshot_status(options)))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}

		if (request.op == "cancel" || request.op == "break") {
			const bool cancelled = request_cancel();
			if (!cancelled) {
				if (!emit_session_line(make_error_json_line(request.id, "no command is running"))) {
					result.had_io_error = true;
					break;
				}
				continue;
			}
			if (!emit_session_line(make_cancel_result_json_line(request.id, true))) {
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
		std::string stage_error = {};
		if (!stage_exec_input(request, stage_error)) {
			active_request_id.clear();
			if (!emit_session_line(make_error_json_line(request.id, stage_error))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}
		reset_max_errorlevel();
		cancel_requested.store(false);
		exec_in_progress.store(true);
		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		exec_in_progress.store(false);
		command_result.cancelled = cancel_requested.exchange(false);
		if (command_result.cancelled) {
			clear_queued_input();
		}
		const auto end_ms = get_monotonic_ms();
		command_result.duration_ms = end_ms >= start_ms ? (end_ms - start_ms) : 0;
		if (is_duplex_session_disconnected(state, peer_disconnected)) {
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

	if (!result.had_io_error &&
	    !is_duplex_session_disconnected(state, peer_disconnected) &&
	    !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
		result.had_io_error = true;
	}

	if (stop_reader) {
		stop_reader();
	}
	if (reader.joinable()) {
		reader.join();
	}

	{
		std::lock_guard<std::mutex> lock(state.mutex);
		result.had_io_error = result.had_io_error || state.had_io_error;
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

		if (request.op == "cancel" || request.op == "break") {
			const bool cancelled = request_cancel();
			if (!cancelled) {
				if (!emit_session_line(make_error_json_line(request.id, "no command is running"))) {
					result.had_io_error = true;
					break;
				}
				continue;
			}
			if (!emit_session_line(make_cancel_result_json_line(request.id, true))) {
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
		std::string stage_error = {};
		if (!stage_exec_input(request, stage_error)) {
			active_request_id.clear();
			if (!emit_session_line(make_error_json_line(request.id, stage_error))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}
		reset_max_errorlevel();
		cancel_requested.store(false);
		exec_in_progress.store(true);
		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		exec_in_progress.store(false);
		command_result.cancelled = cancel_requested.exchange(false);
		if (command_result.cancelled) {
			clear_queued_input();
		}
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

	result = run_control_duplex_session(
	        options,
	        [&state](std::string &line) {
			const bool read = read_pipe_line(state, line);
			if (!read) {
				std::lock_guard<std::mutex> lock(state.mutex);
				state.disconnected = true;
			}
			return read;
		},
	        [&state](const std::string &line) {
			return write_pipe_session_line(state, line);
		},
	        [&state]() { return is_pipe_session_disconnected(state); },
	        [&state]() { stop_pipe_session_reader(state); },
	        exec_request);

	close_fd(state.read_fd);
	close_fd(state.write_fd);
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

#if defined(_WIN32) || defined(WIN32)
	PipeServer server{};
	std::string error = {};
	if (!open_pipe_server(control->opt_host_control.endpoint, server, error)) {
		std::fprintf(stderr, "%s\n", error.c_str());
		std::fflush(stderr);
		return false;
	}
	if (!connect_pipe_server(server, error)) {
		close_pipe_server(server);
		std::fprintf(stderr, "%s\n", error.c_str());
		std::fflush(stderr);
		return false;
	}

	WindowsPipeSessionState state = {};
	state.handle = pipe_server_handle(server);
	state.endpoint = server.base_path;
	if (!open_windows_pipe_session(state)) {
		close_windows_pipe_session(state);
		close_pipe_server(server);
		const auto session_error = windows_pipe_session_error(state);
		std::fprintf(stderr, "%s\n", session_error.c_str());
		std::fflush(stderr);
		return false;
	}
	const auto result = run_control_duplex_session(
	        control->opt_host_control,
	        [&state](std::string &line) { return read_windows_pipe_line(state, line); },
	        [&state](const std::string &line) { return write_windows_pipe_line(state, line); },
	        [&state]() { return state.disconnected.load(); },
	        [&state]() { stop_windows_pipe_reader(state); },
	        [](const Request &request, CommandResult &result) {
		        const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
		        if (ok) {
			        populate_command_result(result);
		        }
		        return ok;
	        });

	const auto session_error = windows_pipe_session_error(state);
	close_windows_pipe_session(state);
	close_pipe_server(server);
	if (!session_error.empty()) {
		std::fprintf(stderr, "%s\n", session_error.c_str());
		std::fflush(stderr);
	}
	return result.started && session_error.empty();
#elif !defined(__unix__) && !defined(__APPLE__)
	std::fprintf(stderr, "Host control pipe transport is unsupported on this platform\n");
	std::fflush(stderr);
	return false;
#else
	PipeServer server{};
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

static void reset_pipe_server(PipeServer &server)
{
	server.input_fd = -1;
	server.native_handle = 0;
	server.connect_event = 0;
	server.stop_requested.store(false);
	server.base_path.clear();
	server.input_path.clear();
	server.output_path.clear();
	server.created_input_path = false;
	server.created_output_path = false;
	server.connected = false;
}

bool open_pipe_server(const std::string &path, PipeServer &server, std::string &error)
{
	reset_pipe_server(server);
	error.clear();

#if defined(_WIN32) || defined(WIN32)
	if (path.empty()) {
		error = "host control pipe path is empty";
		return false;
	}

	server.base_path = normalize_windows_pipe_endpoint(path, error);
	if (!error.empty()) {
		return false;
	}

	std::wstring endpoint = {};
	if (!utf8_to_wide(server.base_path, endpoint)) {
		error = "host control pipe endpoint is not valid UTF-8: " + server.base_path;
		return false;
	}

	PSECURITY_DESCRIPTOR descriptor = nullptr;
	if (!build_windows_pipe_security_descriptor(server.base_path, error, descriptor)) {
		return false;
	}
	SecurityDescriptorHolder descriptor_holder;
	descriptor_holder.reset(descriptor);

	SECURITY_ATTRIBUTES attributes = {};
	attributes.nLength = sizeof(attributes);
	attributes.lpSecurityDescriptor = descriptor_holder.get();
	attributes.bInheritHandle = FALSE;

	const auto handle = CreateNamedPipeW(
	        endpoint.c_str(),
	        PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
	        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
	        1,
	        4096,
	        4096,
	        0,
	        &attributes);
	if (handle == INVALID_HANDLE_VALUE) {
		error = make_win32_message("failed to create host control pipe",
	                           server.base_path,
	                           GetLastError());
		return false;
	}

	server.native_handle = reinterpret_cast<std::uintptr_t>(handle);
	return true;
#elif !defined(__unix__) && !defined(__APPLE__)
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
#if defined(_WIN32) || defined(WIN32)
	server.stop_requested.store(true);
	if (server.native_handle != 0) {
		const auto handle = pipe_server_handle(server);
		if (server.connect_event != 0) {
			(void)CancelIoEx(handle, nullptr);
		}
		if (server.connected) {
			(void)DisconnectNamedPipe(handle);
		}
		(void)CloseHandle(handle);
	}
	if (server.connect_event != 0) {
		(void)CloseHandle(reinterpret_cast<HANDLE>(server.connect_event));
	}
#elif defined(__unix__) || defined(__APPLE__)
	close_fd(server.input_fd);
	if (server.created_input_path && !server.input_path.empty()) {
		(void)unlink(server.input_path.c_str());
	}
	if (server.created_output_path && !server.output_path.empty()) {
		(void)unlink(server.output_path.c_str());
	}
#endif
	reset_pipe_server(server);
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
		if (byte == '\t') {
			codes.push_back(0x0f09);
			continue;
		}
		if (byte == '\b') {
			codes.push_back(0x0e08);
			continue;
		}
		if (byte == 0x03) { /* Ctrl-C */
			codes.push_back(0x2e03);
			continue;
		}
		if (byte == 0x1a) { /* Ctrl-Z */
			codes.push_back(0x2c1a);
			continue;
		}
		if (byte < 0x20 || byte > 0x7e) {
			error = "input_text supports printable ASCII plus tab, backspace, Ctrl-C, and Ctrl-Z";
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

void flush_buffered_output_if_due()
{
	if (!session_active || active_request_id.empty()) {
		return;
	}

	const auto now_ms = get_monotonic_ms();
	if (should_flush_buffered_output(
	            buffered_output, now_ms, buffered_output_max_bytes, buffered_output_max_ms)) {
		(void)emit_session_line(flush_buffered_output_json_line(buffered_output));
	}
}

void capture_tty_output(const uint8_t byte)
{
	if (!session_active || active_request_id.empty()) {
		return;
	}

	if (console_device_capture_active) {
		return;
	}

	const auto now_ms = get_monotonic_ms();
	append_buffered_output(buffered_output, &byte, 1, now_ms);
	if (should_flush_buffered_output(
	            buffered_output, now_ms, buffered_output_max_bytes, buffered_output_max_ms)) {
		(void)emit_session_line(flush_buffered_output_json_line(buffered_output));
	}
}

void set_console_device_capture(const bool active)
{
	console_device_capture_active = active;
}

void reset_max_errorlevel()
{
	max_errorlevel_seen = 0;
}

void track_dos_errorlevel(const uint16_t errorlevel)
{
	if (errorlevel > max_errorlevel_seen) {
		max_errorlevel_seen = errorlevel;
	}
}

uint16_t take_max_errorlevel()
{
	const auto value = max_errorlevel_seen;
	max_errorlevel_seen = 0;
	return value;
}

} // namespace host_control
