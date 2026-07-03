#include "dosbox.h"
#include "host_control.h"

#include <gtest/gtest.h>

#include <cstdio>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <unistd.h>
#endif

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

TEST(HostControlProtocolTest, RejectsUnsupportedOperation)
{
	const auto request = host_control::parse_request_line(R"({"id":"42","op":"shutdown"})");

	EXPECT_FALSE(request.ok);
	EXPECT_EQ(request.id, "42");
	EXPECT_EQ(request.error, "unsupported op");
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
		result.duration_ms = 12;
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
	EXPECT_EQ(writes[2],
	          "{\"event\":\"result\",\"id\":\"9\",\"ok\":true,\"shell_exit\":false,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\BUILD\",\"duration_ms\":12}\n");
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
