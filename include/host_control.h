#ifndef DOSBOX_HOST_CONTROL_H
#define DOSBOX_HOST_CONTROL_H

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>

namespace host_control {

enum class Transport {
	Disabled,
	Stdio,
	Socket,
	Pipe,
};

struct Options {
	Transport transport = Transport::Disabled;
	std::string endpoint = {};
};

struct Request {
	bool ok = false;
	std::string id = {};
	std::string op = {};
	std::string command = {};
	std::string error = {};
};

struct BufferedOutput {
	std::string request_id = {};
	std::string bytes = {};
	uint64_t first_byte_ms = 0;
};

struct CommandResult {
	bool shell_exit = false;
	uint32_t errorlevel = 0;
	std::string drive = {};
	std::string cwd = {};
	uint64_t duration_ms = 0;
};

struct StatusSnapshot {
	Transport transport = Transport::Disabled;
	bool session_active = false;
	uint32_t errorlevel = 0;
	std::string drive = {};
	std::string cwd = {};
};

struct SessionResult {
	bool started = false;
	bool had_io_error = false;
};

struct SocketServer {
	int listen_fd = -1;
	std::string path = {};
	bool created_path = false;
};

using ReadLineFn = std::function<bool(std::string &)>;
using WriteLineFn = std::function<bool(const std::string &)>;
using ExecRequestFn = std::function<bool(const Request &, CommandResult &)>;

inline const char *transport_to_string(const Transport transport)
{
	switch (transport) {
	case Transport::Disabled: return "disabled";
	case Transport::Stdio: return "stdio";
	case Transport::Socket: return "socket";
	case Transport::Pipe: return "pipe";
	}
	return "disabled";
}

inline std::string json_escape(const std::string &value)
{
	std::string escaped;
	escaped.reserve(value.size());

	for (const auto c : value) {
		switch (c) {
		case '\\': escaped += "\\\\"; break;
		case '"': escaped += "\\\""; break;
		case '\n': escaped += "\\n"; break;
		case '\r': escaped += "\\r"; break;
		case '\t': escaped += "\\t"; break;
		default: escaped += c; break;
		}
	}

	return escaped;
}

inline std::string make_ready_json_line(const Options &options)
{
	std::string json = "{\"event\":\"ready\",\"transport\":\"";
	json += transport_to_string(options.transport);
	json += "\"";

	if (!options.endpoint.empty()) {
		json += ",\"endpoint\":\"";
		json += json_escape(options.endpoint);
		json += "\"";
	}

	json += "}\n";
	return json;
}

Request parse_request_line(const std::string &line);
std::string make_error_json_line(const std::string &id, const std::string &message);
std::string make_output_json_line(const std::string &id, const std::string &text);
std::string make_output_bytes_json_line(const std::string &id, const uint8_t *data, std::size_t size);
std::string make_exec_result_json_line(const std::string &id,
                                       bool ok,
                                       const CommandResult &result);
std::string make_status_json_line(const std::string &id, const StatusSnapshot &snapshot);
void reset_buffered_output(BufferedOutput &buffer, const std::string &request_id);
void append_buffered_output(BufferedOutput &buffer, const uint8_t *data, std::size_t size, uint64_t now_ms);
bool has_buffered_output(const BufferedOutput &buffer);
bool should_flush_buffered_output(const BufferedOutput &buffer,
                                  uint64_t now_ms,
                                  std::size_t max_bytes = 4096,
                                  uint64_t max_ms = 100);
std::string flush_buffered_output_json_line(BufferedOutput &buffer);
bool should_capture_dos_write(uint16_t info, const char *name);
bool is_stdio_enabled(const Options &options);
bool is_socket_enabled(const Options &options);
bool is_pipe_enabled(const Options &options);
SessionResult run_control_session(const Options &options,
                                  const ReadLineFn &read_line,
                                  const WriteLineFn &write_line,
                                  const ExecRequestFn &exec_request);
bool run_stdio_shell();
bool run_pipe_shell();
bool open_socket_server(const std::string &path, SocketServer &server, std::string &error);
void close_socket_server(SocketServer &server);
bool run_socket_shell();
void capture_dos_write(uint16_t info, const char *name, const uint8_t *data, std::size_t size);

} // namespace host_control

#endif
