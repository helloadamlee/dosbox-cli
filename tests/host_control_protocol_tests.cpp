#include "dosbox.h"
#include "dos_inc.h"
#include "host_control.h"

#include <gtest/gtest.h>

#include <chrono>
#include <cstring>
#include <cstdio>
#include <thread>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <unistd.h>
#endif

#include "../src/dos/drives.h"
#include "dosbox_test_fixture.h"

bool DOSBOX_parse_argv();

namespace {

#if defined(__unix__) || defined(__APPLE__)
std::string make_temp_socket_path()
{
	char templ[] = "/tmp/dosboxx-host-control-XXXXXX";
	const auto dir = mkdtemp(templ);
	EXPECT_NE(dir, nullptr);
	return std::string(dir ? dir : "/tmp") + "/control.sock";
}
#endif

class ScopedHostControlDosState {
public:
	ScopedHostControlDosState()
	        : default_drive(DOS_GetDefaultDrive()),
	          errorlevel(dos.return_code),
	          z_drive_curdir(Drives[25] ? Drives[25]->curdir : "")
	{}

	~ScopedHostControlDosState()
	{
		dos.return_code = errorlevel;
		if (Drives[25] != nullptr) {
			std::strcpy(Drives[25]->curdir, z_drive_curdir.c_str());
		}
		DOS_SetDefaultDrive(default_drive);
	}

private:
	uint8_t default_drive = 0;
	uint16_t errorlevel = 0;
	std::string z_drive_curdir = {};
};

class DOSBox_HostControlArgvTest : public DOSBoxTestFixture {
protected:
	void TearDown() override
	{
		delete control;
		control = saved_control;
		saved_control = nullptr;

		delete parsed_cmdline;
		parsed_cmdline = nullptr;
	}

	void ParseArgs(const char *cmdline)
	{
		saved_control = control;
		parsed_cmdline = new CommandLine("dosbox-x", cmdline, CommandLine::either_except);
		control = new Config(parsed_cmdline);
		ASSERT_TRUE(DOSBOX_parse_argv());
	}

private:
	Config *saved_control = nullptr;
	CommandLine *parsed_cmdline = nullptr;
};

TEST_F(DOSBox_HostControlArgvTest, HostControlDefaultsDisabled)
{
	ParseArgs("");
	EXPECT_EQ(control->opt_host_control.transport, host_control::Transport::Disabled);
	EXPECT_TRUE(control->opt_host_control.endpoint.empty());
	EXPECT_FALSE(control->opt_headless);
}

TEST_F(DOSBox_HostControlArgvTest, ParsesControlStdio)
{
	ParseArgs("-control-stdio");
	EXPECT_EQ(control->opt_host_control.transport, host_control::Transport::Stdio);
	EXPECT_TRUE(control->opt_host_control.endpoint.empty());
}

TEST_F(DOSBox_HostControlArgvTest, ParsesControlSocketPath)
{
	ParseArgs("-control-socket /tmp/dosboxx.sock");
	EXPECT_EQ(control->opt_host_control.transport, host_control::Transport::Socket);
	EXPECT_EQ(control->opt_host_control.endpoint, "/tmp/dosboxx.sock");
}

TEST_F(DOSBox_HostControlArgvTest, ParsesControlPipePath)
{
	ParseArgs("-control-pipe /tmp/dosboxx.pipe");
	EXPECT_EQ(control->opt_host_control.transport, host_control::Transport::Pipe);
	EXPECT_EQ(control->opt_host_control.endpoint, "/tmp/dosboxx.pipe");
	EXPECT_TRUE(host_control::is_pipe_enabled(control->opt_host_control));
	EXPECT_FALSE(host_control::is_stdio_enabled(control->opt_host_control));
	EXPECT_FALSE(host_control::is_socket_enabled(control->opt_host_control));
}

TEST_F(DOSBox_HostControlArgvTest, ParsesHeadlessFlag)
{
	ParseArgs("-headless");
	EXPECT_TRUE(control->opt_headless);
	EXPECT_TRUE(control->opt_nomenu);
	EXPECT_TRUE(control->opt_fastlaunch);
}

TEST(HostControlProtocolTest, ReadyLineEscapesEndpoint)
{
	host_control::Options options;
	options.transport = host_control::Transport::Socket;
	options.endpoint = "/tmp/dosboxx \"control\"\n.sock";

	EXPECT_EQ(host_control::make_ready_json_line(options),
	          "{\"event\":\"ready\",\"transport\":\"socket\",\"endpoint\":\"/tmp/dosboxx \\\"control\\\"\\n.sock\"}\n");
}

TEST(HostControlProtocolTest, ParsesExecRequest)
{
	const auto request = host_control::parse_request_line(R"({"id":"42","op":"exec","command":"dir"})");

	EXPECT_TRUE(request.ok);
	EXPECT_EQ(request.id, "42");
	EXPECT_EQ(request.op, "exec");
	EXPECT_EQ(request.command, "dir");
	EXPECT_TRUE(request.error.empty());
}

TEST(HostControlProtocolTest, ParsesStatusRequestWithoutCommand)
{
	const auto request = host_control::parse_request_line(R"({"id":"42","op":"status"})");

	EXPECT_TRUE(request.ok);
	EXPECT_EQ(request.id, "42");
	EXPECT_EQ(request.op, "status");
	EXPECT_TRUE(request.command.empty());
	EXPECT_TRUE(request.error.empty());
}

TEST(HostControlProtocolTest, RejectsUnsupportedOperation)
{
	const auto request = host_control::parse_request_line(R"({"id":"42","op":"shutdown"})");

	EXPECT_FALSE(request.ok);
	EXPECT_EQ(request.id, "42");
	EXPECT_EQ(request.error, "unsupported op");
}

TEST(HostControlProtocolTest, StatusLineReportsSessionState)
{
	host_control::StatusSnapshot status = {};
	status.transport = host_control::Transport::Socket;
	status.session_active = true;
	status.errorlevel = 7;
	status.drive = "Z";
	status.cwd = "Z:\\BUILD";

	EXPECT_EQ(host_control::make_status_json_line("42", status),
	          "{\"event\":\"status\",\"id\":\"42\",\"transport\":\"socket\",\"session_active\":true,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\BUILD\"}\n");
}

TEST(HostControlProtocolTest, StatusLineEscapesPathFields)
{
	host_control::StatusSnapshot status = {};
	status.transport = host_control::Transport::Stdio;
	status.session_active = true;
	status.errorlevel = 0;
	status.drive = "C";
	status.cwd = "C:\\TMP\\\"QUOTED\"";

	EXPECT_EQ(host_control::make_status_json_line("77", status),
	          "{\"event\":\"status\",\"id\":\"77\",\"transport\":\"stdio\",\"session_active\":true,\"errorlevel\":0,\"drive\":\"C\",\"cwd\":\"C:\\\\TMP\\\\\\\"QUOTED\\\"\"}\n");
}

TEST(HostControlProtocolTest, OutputLineEscapesPayload)
{
	EXPECT_EQ(host_control::make_output_json_line("42", "line 1\n\"quoted\""),
	          "{\"event\":\"output\",\"id\":\"42\",\"text\":\"line 1\\n\\\"quoted\\\"\"}\n");
}

TEST(HostControlProtocolTest, ExecResultLineReportsShellState)
{
	host_control::CommandResult result = {};
	result.shell_exit = false;
	result.errorlevel = 0;
	result.drive = "C";
	result.cwd = "C:\\XCODE101L";
	result.duration_ms = 183;

	EXPECT_EQ(host_control::make_exec_result_json_line("42", true, result),
	          "{\"event\":\"result\",\"id\":\"42\",\"ok\":true,\"shell_exit\":false,\"errorlevel\":0,\"drive\":\"C\",\"cwd\":\"C:\\\\XCODE101L\",\"duration_ms\":183}\n");

	result.shell_exit = true;
	result.errorlevel = 7;
	result.drive = "Z";
	result.cwd = "Z:\\";
	result.duration_ms = 0;

	EXPECT_EQ(host_control::make_exec_result_json_line("43", false, result),
	          "{\"event\":\"result\",\"id\":\"43\",\"ok\":false,\"shell_exit\":true,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\\",\"duration_ms\":0}\n");
}

TEST(HostControlProtocolTest, ExecResultEscapesStructuredPathFields)
{
	host_control::CommandResult result = {};
	result.shell_exit = false;
	result.errorlevel = 3;
	result.drive = "C";
	result.cwd = "C:\\TMP\\\"QUOTED\"";
	result.duration_ms = 44;

	EXPECT_EQ(host_control::make_exec_result_json_line("77", true, result),
	          "{\"event\":\"result\",\"id\":\"77\",\"ok\":true,\"shell_exit\":false,\"errorlevel\":3,\"drive\":\"C\",\"cwd\":\"C:\\\\TMP\\\\\\\"QUOTED\\\"\",\"duration_ms\":44}\n");
}

TEST(HostControlProtocolTest, OutputBytesUseBase64Encoding)
{
	const uint8_t bytes[] = {'A', 0x00, 0xff};

	EXPECT_EQ(host_control::make_output_bytes_json_line("42", bytes, sizeof(bytes)),
	          "{\"event\":\"output\",\"id\":\"42\",\"encoding\":\"base64\",\"data\":\"QQD/\"}\n");
}

TEST(HostControlProtocolTest, CapturesConsoleDeviceWrites)
{
	EXPECT_TRUE(host_control::should_capture_dos_write(
	        DeviceInfoFlags::Device | DeviceInfoFlags::StdOut, "CON"));
	EXPECT_TRUE(host_control::should_capture_dos_write(
	        DeviceInfoFlags::Device | DeviceInfoFlags::StdOut, "con"));
}

TEST(HostControlProtocolTest, IgnoresNonConsoleWrites)
{
	EXPECT_FALSE(host_control::should_capture_dos_write(0, "BUILD.LOG"));
	EXPECT_FALSE(host_control::should_capture_dos_write(
	        DeviceInfoFlags::Device | DeviceInfoFlags::StdOut, "PRN"));
}

TEST(HostControlProtocolTest, BufferedOutputCoalescesAdjacentWrites)
{
	host_control::BufferedOutput buffer = {};
	const uint8_t h[] = {'h'};
	const uint8_t i[] = {'i'};
	const uint8_t crlf[] = {'\r', '\n'};

	host_control::reset_buffered_output(buffer, "42");
	host_control::append_buffered_output(buffer, h, sizeof(h), 10);
	host_control::append_buffered_output(buffer, i, sizeof(i), 20);
	host_control::append_buffered_output(buffer, crlf, sizeof(crlf), 30);

	EXPECT_TRUE(host_control::has_buffered_output(buffer));
	EXPECT_EQ(host_control::flush_buffered_output_json_line(buffer),
	          "{\"event\":\"output\",\"id\":\"42\",\"encoding\":\"base64\",\"data\":\"aGkNCg==\"}\n");
	EXPECT_FALSE(host_control::has_buffered_output(buffer));
}

TEST(HostControlProtocolTest, BufferedOutputKeepsRequestIdAcrossFlushes)
{
	host_control::BufferedOutput buffer = {};
	const uint8_t bytes[] = {'o', 'k'};

	host_control::reset_buffered_output(buffer, "7");
	host_control::append_buffered_output(buffer, bytes, sizeof(bytes), 10);
	(void)host_control::flush_buffered_output_json_line(buffer);

	EXPECT_EQ(buffer.request_id, "7");
	EXPECT_TRUE(buffer.bytes.empty());
}

TEST(HostControlProtocolTest, BufferedOutputFlushesAtSizeThreshold)
{
	host_control::BufferedOutput buffer = {};
	const uint8_t bytes[] = {'a', 'b', 'c', 'd'};

	host_control::reset_buffered_output(buffer, "9");
	host_control::append_buffered_output(buffer, bytes, sizeof(bytes), 10);

	EXPECT_TRUE(host_control::should_flush_buffered_output(buffer, 10, 4, 100));
	EXPECT_FALSE(host_control::should_flush_buffered_output(buffer, 10, 5, 100));
}

TEST(HostControlProtocolTest, BufferedOutputFlushesAtTimeThreshold)
{
	host_control::BufferedOutput buffer = {};
	const uint8_t bytes[] = {'a'};

	host_control::reset_buffered_output(buffer, "9");
	host_control::append_buffered_output(buffer, bytes, sizeof(bytes), 25);

	EXPECT_FALSE(host_control::should_flush_buffered_output(buffer, 124, 4096, 100));
	EXPECT_TRUE(host_control::should_flush_buffered_output(buffer, 125, 4096, 100));
}

TEST(HostControlProtocolTest, SessionRunnerEmitsReadyAndInvalidRequestError)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"42","op":"shutdown"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			line.clear();
			return false;
		}

		line = requests[next_request++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};
	const auto exec_request = [&](const host_control::Request &,
	                              host_control::CommandResult &result) {
		result.shell_exit = false;
		return false;
	};

	const host_control::Options options = {
	        host_control::Transport::Socket,
	        "/tmp/dosboxx.sock",
	};

	const auto result = host_control::run_control_session(
	        options, read_line, write_line, exec_request);

	EXPECT_TRUE(result.started);
	EXPECT_FALSE(result.had_io_error);
	ASSERT_EQ(writes.size(), 2u);
	EXPECT_EQ(writes[0],
	          "{\"event\":\"ready\",\"transport\":\"socket\",\"endpoint\":\"/tmp/dosboxx.sock\"}\n");
	EXPECT_EQ(writes[1], "{\"event\":\"error\",\"id\":\"42\",\"message\":\"unsupported op\"}\n");
}

TEST(HostControlProtocolTest, SessionRunnerEmitsStatusWithoutResult)
{
	const ScopedHostControlDosState dos_state = {};
	ASSERT_NE(Drives[25], nullptr);
	DOS_SetDefaultDrive(25);
	std::strcpy(Drives[25]->curdir, "");
	dos.return_code = 0;

	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"7","op":"status"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			line.clear();
			return false;
		}
		line = requests[next_request++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};
	const auto exec_request = [&](const host_control::Request &, host_control::CommandResult &) {
		ADD_FAILURE() << "status must not execute shell commands";
		return false;
	};

	const auto session = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Socket, "/tmp/d.sock"},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(session.started);
	EXPECT_FALSE(session.had_io_error);
	ASSERT_EQ(writes.size(), 2u);
	EXPECT_EQ(writes[0],
	          "{\"event\":\"ready\",\"transport\":\"socket\",\"endpoint\":\"/tmp/d.sock\"}\n");
	EXPECT_EQ(writes[1],
	          "{\"event\":\"status\",\"id\":\"7\",\"transport\":\"socket\",\"session_active\":true,\"errorlevel\":0,\"drive\":\"Z\",\"cwd\":\"Z:\\\\\"}\n");
}

TEST(HostControlProtocolTest, SessionRunnerStatusReflectsUpdatedStateBetweenCommands)
{
	const ScopedHostControlDosState dos_state = {};
	ASSERT_NE(Drives[25], nullptr);
	DOS_SetDefaultDrive(25);
	std::strcpy(Drives[25]->curdir, "");
	dos.return_code = 0;

	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {
	        R"({"id":"1","op":"exec","command":"cd \\build"})",
	        R"({"id":"2","op":"status"})",
	};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			line.clear();
			return false;
		}
		line = requests[next_request++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};
	const auto exec_request = [&](const host_control::Request &request,
	                              host_control::CommandResult &result) {
		EXPECT_EQ(request.op, "exec");
		EXPECT_EQ(request.command, "cd \\build");
		DOS_SetDefaultDrive(25);
		std::strcpy(Drives[25]->curdir, "BUILD");
		dos.return_code = 7;
		result.shell_exit = false;
		result.errorlevel = 7;
		result.drive = "Z";
		result.cwd = "Z:\\BUILD";
		return true;
	};

	const auto session = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Stdio, ""},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(session.started);
	EXPECT_FALSE(session.had_io_error);
	ASSERT_EQ(writes.size(), 3u);
	EXPECT_EQ(writes[0], "{\"event\":\"ready\",\"transport\":\"stdio\"}\n");
	EXPECT_NE(writes[1].find("\"event\":\"result\""), std::string::npos);
	EXPECT_EQ(writes[2],
	          "{\"event\":\"status\",\"id\":\"2\",\"transport\":\"stdio\",\"session_active\":true,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\BUILD\"}\n");
}

TEST(HostControlProtocolTest, SessionRunnerFlushesBufferedOutputBeforeStructuredResult)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"9","op":"exec","command":"echo hi"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			line.clear();
			return false;
		}

		line = requests[next_request++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};
	const auto exec_request = [&](const host_control::Request &request,
	                              host_control::CommandResult &result) {
		const uint8_t bytes[] = {'h', 'i', '\r', '\n'};
		EXPECT_EQ(request.id, "9");
		EXPECT_EQ(request.command, "echo hi");
		result.shell_exit = false;
		result.errorlevel = 7;
		result.drive = "Z";
		result.cwd = "Z:\\BUILD";
		host_control::capture_dos_write(
		        DeviceInfoFlags::Device | DeviceInfoFlags::StdOut,
		        "CON",
		        bytes,
		        sizeof(bytes));
		return true;
	};

	const auto result = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Socket, "/tmp/d.sock"},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(result.started);
	EXPECT_FALSE(result.had_io_error);
	ASSERT_EQ(writes.size(), 3u);
	EXPECT_EQ(writes[0],
	          "{\"event\":\"ready\",\"transport\":\"socket\",\"endpoint\":\"/tmp/d.sock\"}\n");
	EXPECT_EQ(writes[1],
	          "{\"event\":\"output\",\"id\":\"9\",\"encoding\":\"base64\",\"data\":\"aGkNCg==\"}\n");
	EXPECT_NE(writes[2].find("\"event\":\"result\""), std::string::npos);
	EXPECT_NE(writes[2].find("\"id\":\"9\""), std::string::npos);
	EXPECT_NE(writes[2].find("\"ok\":true"), std::string::npos);
	EXPECT_NE(writes[2].find("\"shell_exit\":false"), std::string::npos);
	EXPECT_NE(writes[2].find("\"errorlevel\":7"), std::string::npos);
	EXPECT_NE(writes[2].find("\"drive\":\"Z\""), std::string::npos);
	EXPECT_NE(writes[2].find("\"cwd\":\"Z:\\\\BUILD\""), std::string::npos);
	EXPECT_NE(writes[2].find("\"duration_ms\":"), std::string::npos);
}

TEST(HostControlProtocolTest, SessionRunnerEmitsStructuredResultMetadata)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"11","op":"exec","command":"cd \\build"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			line.clear();
			return false;
		}

		line = requests[next_request++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};
	const auto exec_request = [&](const host_control::Request &,
	                              host_control::CommandResult &result) {
		std::this_thread::sleep_for(std::chrono::milliseconds(2));
		result.shell_exit = false;
		result.errorlevel = 1;
		result.drive = "Z";
		result.cwd = "Z:\\BUILD";
		return true;
	};

	const auto session = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Stdio, ""},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(session.started);
	EXPECT_FALSE(session.had_io_error);
	ASSERT_EQ(writes.size(), 2u);
	EXPECT_NE(writes[1].find("\"errorlevel\":1"), std::string::npos);
	EXPECT_NE(writes[1].find("\"drive\":\"Z\""), std::string::npos);
	EXPECT_NE(writes[1].find("\"cwd\":\"Z:\\\\BUILD\""), std::string::npos);
	EXPECT_EQ(writes[1].find("\"duration_ms\":0"), std::string::npos);
}

#if defined(__unix__) || defined(__APPLE__)
TEST(HostControlProtocolTest, SocketServerRejectsEmptyEndpoint)
{
	host_control::SocketServer server = {};
	std::string error = {};

	EXPECT_FALSE(host_control::open_socket_server("", server, error));
	EXPECT_NE(error.find("empty"), std::string::npos);
}

TEST(HostControlProtocolTest, SocketServerRemovesStalePathAndCleansUpOnClose)
{
	const auto socket_path = make_temp_socket_path();
	const auto slash = socket_path.find_last_of('/');
	ASSERT_NE(slash, std::string::npos);
	const auto socket_dir = socket_path.substr(0, slash);
	const auto stale = std::fopen(socket_path.c_str(), "w");
	ASSERT_NE(stale, nullptr);
	std::fclose(stale);
	ASSERT_EQ(access(socket_path.c_str(), F_OK), 0);

	host_control::SocketServer server = {};
	std::string error = {};
	ASSERT_TRUE(host_control::open_socket_server(socket_path, server, error)) << error;
	EXPECT_EQ(access(socket_path.c_str(), F_OK), 0);

	host_control::close_socket_server(server);
	EXPECT_NE(access(socket_path.c_str(), F_OK), 0);
	EXPECT_EQ(rmdir(socket_dir.c_str()), 0);
}
#endif

} // namespace
