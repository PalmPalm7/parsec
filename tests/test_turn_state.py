import asyncio
import socket
import ssl

import httpx
import pytest

from src.connections import turn_state


def test_nothing_is_remembered_outside_a_turn():
    turn_state.mark_dead("aap2:prod0", "401")
    assert turn_state.dead_reason("aap2:prod0") is None


def test_a_turn_remembers_and_a_new_turn_forgets():
    token = turn_state.begin_turn()
    try:
        turn_state.mark_dead("aap2:prod0", "401")
        turn_state.mark_dead("aap2:prod0", "second reason is ignored")
        assert turn_state.dead_reason("aap2:prod0") == "401"
    finally:
        turn_state.end_turn(token)
    token = turn_state.begin_turn()
    try:
        assert turn_state.dead_reason("aap2:prod0") is None
    finally:
        turn_state.end_turn(token)


# ---------------------------------------------------------------------------
# Which connect failures are final. The exceptions come from the real httpx /
# httpcore / anyio stack, so a change in how those libraries chain the OS error
# shows up here and not as a breaker that quietly stops remembering anything.
# ---------------------------------------------------------------------------


async def _connect_error(url: str) -> httpx.ConnectError:
    # trust_env=False: an HTTPS_PROXY in the environment would turn every
    # failure into a proxy error.
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError) as exc:
            await client.get(url)
    return exc.value


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _resolver_fails_with(monkeypatch, errno: int, text: str) -> None:
    def getaddrinfo(*args, **kwargs):
        raise socket.gaierror(errno, text)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


async def test_a_name_that_does_not_exist_is_final(monkeypatch):
    _resolver_fails_with(monkeypatch, socket.EAI_NONAME, "Name or service not known")
    error = await _connect_error("https://aap2-east.example.com/api/v2/jobs/")
    assert turn_state.permanent_connect_failure(error) == turn_state.HOST_NOT_FOUND


async def test_a_resolver_hiccup_is_not_final(monkeypatch):
    _resolver_fails_with(monkeypatch, socket.EAI_AGAIN, "Temporary failure in name resolution")
    error = await _connect_error("https://aap2-east.example.com/api/v2/jobs/")
    assert turn_state.permanent_connect_failure(error) is None


async def test_a_refused_connection_is_final():
    error = await _connect_error(f"http://127.0.0.1:{_closed_port()}/")
    assert turn_state.permanent_connect_failure(error) == turn_state.CONNECTION_REFUSED


async def test_a_tls_failure_is_not_final():
    async def plain_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(plain_http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        error = await _connect_error(f"https://127.0.0.1:{port}/")
    finally:
        server.close()
        await server.wait_closed()
    assert turn_state.permanent_connect_failure(error) is None


def _chained(cause: BaseException) -> httpx.ConnectError:
    try:
        raise httpx.ConnectError("All connection attempts failed") from cause
    except httpx.ConnectError as e:
        return e


def test_every_address_refused_is_final():
    group = ExceptionGroup(
        "multiple connection attempts failed",
        [ConnectionRefusedError(111, "::1"), ConnectionRefusedError(111, "127.0.0.1")],
    )
    error = _chained(group)
    assert turn_state.permanent_connect_failure(error) == turn_state.CONNECTION_REFUSED


def test_one_address_timing_out_is_not_final():
    group = ExceptionGroup(
        "multiple connection attempts failed",
        [TimeoutError("::1"), ConnectionRefusedError(111, "127.0.0.1")],
    )
    assert turn_state.permanent_connect_failure(_chained(group)) is None


def test_a_tls_error_with_the_eai_noname_errno_is_not_a_dns_failure():
    # On macOS a real SSLEOFError carries errno 8, which is EAI_NONAME there.
    tls = ssl.SSLEOFError(socket.EAI_NONAME, "EOF occurred in violation of protocol")
    assert turn_state.permanent_connect_failure(_chained(tls)) is None


def test_the_context_chain_is_followed_too():
    try:
        try:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        except OSError:
            raise httpx.ConnectError("[Errno -2] Name or service not known")
    except httpx.ConnectError as e:
        error = e
    assert error.__cause__ is None
    assert turn_state.permanent_connect_failure(error) == turn_state.HOST_NOT_FOUND
