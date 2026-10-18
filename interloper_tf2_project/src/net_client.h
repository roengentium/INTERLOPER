// net_client.h
//
// Minimal blocking TCP client that speaks the same protocol as
// test_client.py against the core Python server. Deliberately synchronous:
// this is called from a console command handler, not the frame loop, so a
// short blocking stall while the core server generates+serves a demo is
// acceptable here (a few ms to low tens of ms on localhost).
//
// If that stall ever becomes noticeable, the fix is to make StartInterlope
// kick this off on a worker thread and hand the Recording to the frame loop
// via a mutex-guarded queue - not something to build until it's proven to
// be a real problem.

#ifndef INTERLOPER_NET_CLIENT_H
#define INTERLOPER_NET_CLIENT_H

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "recording.h"

#if defined(_WIN32)
	#include <winsock2.h>
	#include <ws2tcpip.h>
	#pragma comment(lib, "ws2_32.lib")
	typedef SOCKET socket_t;
	#define INTERLOPER_INVALID_SOCKET INVALID_SOCKET
	#define INTERLOPER_CLOSESOCKET(s) closesocket(s)
#else
	#include <sys/socket.h>
	#include <netinet/in.h>
	#include <arpa/inet.h>
	#include <unistd.h>
	typedef int socket_t;
	#define INTERLOPER_INVALID_SOCKET (-1)
	#define INTERLOPER_CLOSESOCKET(s) close(s)
#endif

namespace interloper
{

struct MapBounds
{
	float mins[3];
	float maxs[3];
};

class NetClient
{
public:
	NetClient(const std::string &host, uint16_t commandPort)
		: m_host(host), m_commandPort(commandPort)
	{
#if defined(_WIN32)
		WSADATA wsaData;
		WSAStartup(MAKEWORD(2, 2), &wsaData);
#endif
	}

	~NetClient()
	{
#if defined(_WIN32)
		WSACleanup();
#endif
	}

	// Sends INTERLOPE + the map bounds/name, then pulls and parses the
	// resulting Recording. Returns false on any network or parse failure -
	// caller should treat that as "no demo this time" rather than crash.
	bool RequestDemo(const MapBounds &bounds, const std::string &mapName, const float origin[3], Recording &out, std::string &errorOut, const std::string &options = std::string())
	{
		socket_t cmdSock = Connect(m_host, m_commandPort);
		if (cmdSock == INTERLOPER_INVALID_SOCKET)
		{
			errorOut = "could not connect to core server on " + m_host;
			return false;
		}

		// "INTERLOPE" alone = server picks everything. Anything after it is
		// "<type> key=value ..." (see server.py). Old servers only accept the
		// bare word, new ones accept both.
		std::string line = "INTERLOPE";
		if (!options.empty())
			line += " " + options;
		line += "\n";
		m_lastRequestLine = line;
		if (!SendAll(cmdSock, line.data(), line.size()))
		{
			errorOut = "failed sending INTERLOPE command";
			INTERLOPER_CLOSESOCKET(cmdSock);
			return false;
		}

		if (!SendRequestBlock(cmdSock, bounds, origin, mapName))
		{
			errorOut = "failed sending request block";
			INTERLOPER_CLOSESOCKET(cmdSock);
			return false;
		}

		std::string response;
		if (!RecvLine(cmdSock, response))
		{
			errorOut = "no response from core server";
			INTERLOPER_CLOSESOCKET(cmdSock);
			return false;
		}
		INTERLOPER_CLOSESOCKET(cmdSock);
		m_lastResponseLine = response;

		uint16_t pullPort = 0;
		std::string commitId;
		if (!ParsePullResponse(response, pullPort, commitId))
		{
			errorOut = "unexpected response: " + response;
			return false;
		}

		socket_t pullSock = Connect(m_host, pullPort);
		if (pullSock == INTERLOPER_INVALID_SOCKET)
		{
			errorOut = "could not connect to pull port " + std::to_string(pullPort);
			return false;
		}

		std::vector<uint8_t> payload;
		bool ok = RecvAll(pullSock, payload);
		INTERLOPER_CLOSESOCKET(pullSock);

		if (!ok || payload.empty())
		{
			errorOut = "pull connection returned no data";
			return false;
		}

		if (!ParseRecording(payload.data(), payload.size(), out))
		{
			errorOut = "malformed recording payload";
			return false;
		}

		return true;
	}

	// The exact command line sent, and the server's raw reply, for debug output.
	const std::string &LastRequestLine() const { return m_lastRequestLine; }
	const std::string &LastResponseLine() const { return m_lastResponseLine; }

private:
	std::string m_host;
	uint16_t m_commandPort;
	std::string m_lastRequestLine;
	std::string m_lastResponseLine;

	static socket_t Connect(const std::string &host, uint16_t port)
	{
		socket_t s = socket(AF_INET, SOCK_STREAM, 0);
		if (s == INTERLOPER_INVALID_SOCKET)
			return INTERLOPER_INVALID_SOCKET;

		sockaddr_in addr;
		std::memset(&addr, 0, sizeof(addr));
		addr.sin_family = AF_INET;
		addr.sin_port = htons(port);

		if (inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1)
		{
			INTERLOPER_CLOSESOCKET(s);
			return INTERLOPER_INVALID_SOCKET;
		}

		if (connect(s, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0)
		{
			INTERLOPER_CLOSESOCKET(s);
			return INTERLOPER_INVALID_SOCKET;
		}

		return s;
	}

	static bool SendAll(socket_t s, const void *data, size_t len)
	{
		const char *p = static_cast<const char *>(data);
		size_t sent = 0;
		while (sent < len)
		{
			int n = send(s, p + sent, static_cast<int>(len - sent), 0);
			if (n <= 0)
				return false;
			sent += static_cast<size_t>(n);
		}
		return true;
	}

	static bool SendRequestBlock(socket_t s, const MapBounds &bounds, const float origin[3], const std::string &mapName)
	{
		// Matches server.py's REQUEST_FIXED_FMT = "<I3f3f3f" (name_len, mins,
		// maxs, player origin) followed by the raw map name bytes.
		uint32_t nameLen = static_cast<uint32_t>(mapName.size());

		std::vector<uint8_t> buf;
		buf.resize(sizeof(uint32_t) + sizeof(float) * 9);

		size_t off = 0;
		std::memcpy(buf.data() + off, &nameLen, sizeof(nameLen)); off += sizeof(nameLen);
		std::memcpy(buf.data() + off, bounds.mins, sizeof(bounds.mins)); off += sizeof(bounds.mins);
		std::memcpy(buf.data() + off, bounds.maxs, sizeof(bounds.maxs)); off += sizeof(bounds.maxs);
		std::memcpy(buf.data() + off, origin, sizeof(float) * 3); off += sizeof(float) * 3;

		if (!SendAll(s, buf.data(), buf.size()))
			return false;

		if (!mapName.empty() && !SendAll(s, mapName.data(), mapName.size()))
			return false;

		return true;
	}

	static bool RecvLine(socket_t s, std::string &out)
	{
		out.clear();
		char c;
		while (true)
		{
			int n = recv(s, &c, 1, 0);
			if (n <= 0)
				return !out.empty();
			out += c;
			if (c == '\n')
				return true;
		}
	}

	static bool RecvAll(socket_t s, std::vector<uint8_t> &out)
	{
		out.clear();
		uint8_t buf[4096];
		while (true)
		{
			int n = recv(s, reinterpret_cast<char *>(buf), sizeof(buf), 0);
			if (n < 0)
				return false;
			if (n == 0)
				break; // peer closed - normal end of the pull stream
			out.insert(out.end(), buf, buf + n);
		}
		return true;
	}

	// Response looks like: "s.interlope.pull:27100 13-70-18\n"
	static bool ParsePullResponse(const std::string &line, uint16_t &portOut, std::string &commitOut)
	{
		size_t colon = line.find(':');
		size_t space = line.find(' ');
		if (colon == std::string::npos || space == std::string::npos || space < colon)
			return false;

		std::string portStr = line.substr(colon + 1, space - colon - 1);
		std::string commitStr = line.substr(space + 1);

		// trim trailing newline/whitespace
		while (!commitStr.empty() && (commitStr.back() == '\n' || commitStr.back() == '\r'))
			commitStr.pop_back();

		if (portStr.empty() || commitStr.empty())
			return false;

		portOut = static_cast<uint16_t>(std::atoi(portStr.c_str()));
		commitOut = commitStr;
		return true;
	}
};

} // namespace interloper

#endif // INTERLOPER_NET_CLIENT_H
