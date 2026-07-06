#include "host_control.h"
#include "dos_inc.h"

#include <cctype>
#include <map>

namespace host_control {
namespace {

bool skip_whitespace(const std::string &line, std::size_t &pos)
{
	while (pos < line.size()) {
		const char c = line[pos];
		if (c != ' ' && c != '\t' && c != '\n' && c != '\r') {
			return true;
		}
		++pos;
	}

	return false;
}

bool parse_json_string(const std::string &line, std::size_t &pos, std::string &value)
{
	if (pos >= line.size() || line[pos] != '"') {
		return false;
	}

	++pos;
	value.clear();

	while (pos < line.size()) {
		const char c = line[pos++];

		if (c == '"') {
			return true;
		}

		if (c != '\\') {
			value += c;
			continue;
		}

		if (pos >= line.size()) {
			return false;
		}

		switch (line[pos++]) {
		case '\\': value += '\\'; break;
		case '"': value += '"'; break;
		case 'n': value += '\n'; break;
		case 'r': value += '\r'; break;
		case 't': value += '\t'; break;
		default: return false;
		}
	}

	return false;
}

bool parse_string_map(const std::string &line, std::map<std::string, std::string> &values)
{
	std::size_t pos = 0;
	values.clear();

	if (!skip_whitespace(line, pos) || line[pos] != '{') {
		return false;
	}
	++pos;

	for (;;) {
		if (!skip_whitespace(line, pos)) {
			return false;
		}

		if (line[pos] == '}') {
			++pos;
			break;
		}

		std::string key;
		if (!parse_json_string(line, pos, key)) {
			return false;
		}

		if (!skip_whitespace(line, pos) || line[pos] != ':') {
			return false;
		}
		++pos;

		if (!skip_whitespace(line, pos)) {
			return false;
		}

		std::string value;
		if (!parse_json_string(line, pos, value)) {
			return false;
		}
		values[key] = value;

		if (!skip_whitespace(line, pos)) {
			return false;
		}

		if (line[pos] == '}') {
			++pos;
			break;
		}

		if (line[pos] != ',') {
			return false;
		}
		++pos;
	}

	skip_whitespace(line, pos);
	return pos == line.size();
}

std::string base64_encode(const uint8_t *data, const std::size_t size)
{
	static const char *alphabet =
	        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

	std::string encoded;
	encoded.reserve(((size + 2u) / 3u) * 4u);

	for (std::size_t i = 0; i < size; i += 3u) {
		const std::size_t remaining = size - i;
		const uint32_t chunk = (static_cast<uint32_t>(data[i]) << 16u) |
		                       ((remaining > 1u ? static_cast<uint32_t>(data[i + 1u]) : 0u) << 8u) |
		                       (remaining > 2u ? static_cast<uint32_t>(data[i + 2u]) : 0u);

		encoded += alphabet[(chunk >> 18u) & 0x3fu];
		encoded += alphabet[(chunk >> 12u) & 0x3fu];
		encoded += remaining > 1u ? alphabet[(chunk >> 6u) & 0x3fu] : '=';
		encoded += remaining > 2u ? alphabet[chunk & 0x3fu] : '=';
	}

	return encoded;
}

bool equals_ignore_case(const char *lhs, const char *rhs)
{
	if (lhs == nullptr || rhs == nullptr) {
		return false;
	}

	while (*lhs != '\0' && *rhs != '\0') {
		if (std::tolower(static_cast<unsigned char>(*lhs)) !=
		    std::tolower(static_cast<unsigned char>(*rhs))) {
			return false;
		}
		++lhs;
		++rhs;
	}

	return *lhs == '\0' && *rhs == '\0';
}

} // namespace

Request parse_request_line(const std::string &line)
{
	Request request = {};
	std::map<std::string, std::string> values = {};

	if (!parse_string_map(line, values)) {
		request.error = "invalid request";
		return request;
	}

	const auto id_it = values.find("id");
	if (id_it != values.end()) {
		request.id = id_it->second;
	}

	const auto op_it = values.find("op");
	if (op_it == values.end() || op_it->second.empty()) {
		request.error = "missing op";
		return request;
	}

	request.op = op_it->second;
	if (request.op != "exec" && request.op != "status") {
		request.error = "unsupported op";
		return request;
	}

	if (request.op == "exec") {
		const auto command_it = values.find("command");
		if (command_it == values.end()) {
			request.error = "missing command";
			return request;
		}

		request.command = command_it->second;
	}

	request.ok = true;
	return request;
}

std::string make_error_json_line(const std::string &id, const std::string &message)
{
	std::string json = "{\"event\":\"error\"";

	if (!id.empty()) {
		json += ",\"id\":\"";
		json += json_escape(id);
		json += "\"";
	}

	json += ",\"message\":\"";
	json += json_escape(message);
	json += "\"}\n";
	return json;
}

std::string make_output_json_line(const std::string &id, const std::string &text)
{
	std::string json = "{\"event\":\"output\",\"id\":\"";
	json += json_escape(id);
	json += "\",\"text\":\"";
	json += json_escape(text);
	json += "\"}\n";
	return json;
}

std::string make_output_bytes_json_line(const std::string &id, const uint8_t *data, const std::size_t size)
{
	std::string json = "{\"event\":\"output\",\"id\":\"";
	json += json_escape(id);
	json += "\",\"encoding\":\"base64\",\"data\":\"";
	json += base64_encode(data, size);
	json += "\"}\n";
	return json;
}

std::string make_exec_result_json_line(const std::string &id,
                                       const bool ok,
                                       const CommandResult &result)
{
	std::string json = "{\"event\":\"result\",\"id\":\"";
	json += json_escape(id);
	json += "\",\"ok\":";
	json += ok ? "true" : "false";
	json += ",\"shell_exit\":";
	json += result.shell_exit ? "true" : "false";
	json += ",\"errorlevel\":";
	json += std::to_string(result.errorlevel);
	json += ",\"drive\":\"";
	json += json_escape(result.drive);
	json += "\",\"cwd\":\"";
	json += json_escape(result.cwd);
	json += "\",\"duration_ms\":";
	json += std::to_string(result.duration_ms);
	json += "}\n";
	return json;
}

std::string make_status_json_line(const std::string &id, const StatusSnapshot &snapshot)
{
	std::string json = "{\"event\":\"status\",\"id\":\"";
	json += json_escape(id);
	json += "\",\"transport\":\"";
	json += transport_to_string(snapshot.transport);
	json += "\",\"session_active\":";
	json += snapshot.session_active ? "true" : "false";
	json += ",\"errorlevel\":";
	json += std::to_string(snapshot.errorlevel);
	json += ",\"drive\":\"";
	json += json_escape(snapshot.drive);
	json += "\",\"cwd\":\"";
	json += json_escape(snapshot.cwd);
	json += "\"}\n";
	return json;
}

void reset_buffered_output(BufferedOutput &buffer, const std::string &request_id)
{
	buffer.request_id = request_id;
	buffer.bytes.clear();
	buffer.first_byte_ms = 0;
}

void append_buffered_output(BufferedOutput &buffer,
                            const uint8_t *data,
                            const std::size_t size,
                            const uint64_t now_ms)
{
	if (data == nullptr || size == 0) {
		return;
	}

	if (buffer.bytes.empty()) {
		buffer.first_byte_ms = now_ms;
	}

	buffer.bytes.append(reinterpret_cast<const char *>(data), size);
}

bool has_buffered_output(const BufferedOutput &buffer)
{
	return !buffer.request_id.empty() && !buffer.bytes.empty();
}

bool should_flush_buffered_output(const BufferedOutput &buffer,
                                  const uint64_t now_ms,
                                  const std::size_t max_bytes,
                                  const uint64_t max_ms)
{
	if (!has_buffered_output(buffer)) {
		return false;
	}

	if (buffer.bytes.size() >= max_bytes) {
		return true;
	}

	return now_ms >= buffer.first_byte_ms && now_ms - buffer.first_byte_ms >= max_ms;
}

std::string flush_buffered_output_json_line(BufferedOutput &buffer)
{
	if (!has_buffered_output(buffer)) {
		return {};
	}

	const std::string json = make_output_bytes_json_line(
	        buffer.request_id,
	        reinterpret_cast<const uint8_t *>(buffer.bytes.data()),
	        buffer.bytes.size());
	buffer.bytes.clear();
	buffer.first_byte_ms = 0;
	return json;
}

bool should_capture_dos_write(const uint16_t info, const char *name)
{
	if ((info & DeviceInfoFlags::Device) == 0) {
		return false;
	}

	return equals_ignore_case(name, "CON");
}

bool is_stdio_enabled(const Options &options)
{
	return options.transport == Transport::Stdio;
}

bool is_socket_enabled(const Options &options)
{
	return options.transport == Transport::Socket;
}

bool is_pipe_enabled(const Options &options)
{
	return options.transport == Transport::Pipe;
}

} // namespace host_control
