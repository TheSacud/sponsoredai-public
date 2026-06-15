from __future__ import annotations

import logging
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from .config import FREQUENCY_PROFILES, interactive_terminal
from .status import StatusRenderer


logger = logging.getLogger(__name__)


POSIX_INTERRUPT_GRACE_SECONDS = 1.0
WINDOWS_INTERRUPT_GRACE_SECONDS = 0.5
WINDOWS_KILL_WAIT_SECONDS = 0.5

# Agents that take over the whole screen — an alternate-screen buffer (codex) or a
# pinned, continuously repainted input box (claude). On the Windows passthrough
# path SAI has no ConPTY to coordinate with them, so a bottom-row overlay is
# overwritten within a frame and never seen. For these tools we surface the
# sponsor card inline once, just after launch, instead of fighting for row 999.
FULLSCREEN_TUI_TOOLS = {"claude", "codex"}


def _is_windows() -> bool:
    return os.name == "nt"


@dataclass(frozen=True)
class RunReceipt:
    exit_code: int
    duration_seconds: float
    qualified_waits: int
    credits_earned: float
    balance: float
    session_id: str

    def render(self) -> str:
        return "\n".join(
            [
                "SAI receipt",
                "------------",
                f"Session duration: {format_duration(self.duration_seconds)}",
                f"Qualified sponsored waits: {self.qualified_waits}",
                f"Credits earned: +{self.credits_earned:.3f}",
                "Credits spent this session: -0.000",
                f"Local display balance: {self.balance:.3f}",
            ]
        )


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def configured_idle_seconds(config: dict) -> float:
    name = config.get("frequency", "normal")
    profile = FREQUENCY_PROFILES.get(name, FREQUENCY_PROFILES["normal"])
    return float(profile["idle_seconds"])


class CommandRunner:
    def __init__(self, config: dict, wallet: object | None = None) -> None:
        self.config = config
        self.wallet = wallet
        # Set per-run: True when another surface already holds the billing
        # authority, so this run shows example cards only (credit 0).
        self._credit0 = False

    def _wallet(self):
        if self.wallet is None:
            from .wallet import Wallet

            self.wallet = Wallet()
        return self.wallet

    def _sponsor_session(self, tool: str):
        from .sponsors import SponsorSession

        session = SponsorSession(tool=tool, config=self.config, wallet=self._wallet())
        if self._credit0:
            # Another surface holds the billing authority; show example cards
            # only so this run cannot double-count the same attended wait.
            session.placement_client = None
        return session

    def _empty_receipt(self, start: float, exit_code: int) -> RunReceipt:
        return RunReceipt(
            exit_code=exit_code,
            duration_seconds=time.monotonic() - start,
            qualified_waits=0,
            credits_earned=0.0,
            balance=self._wallet().balance(),
            session_id="",
        )

    def run(self, command: Sequence[str], tool: str = "run") -> RunReceipt:
        if not command:
            raise ValueError("No command provided")

        # Take the shared single billing authority only when this run would
        # actually bill (interactive + sponsors enabled + a backend client). If
        # another surface (e.g. the desktop overlay) already holds it, run
        # credit-0 so one attended wait is never counted twice.
        billing_lock = None
        self._credit0 = False
        if interactive_terminal():
            from .sponsors import RemotePlacementClient, sponsor_enabled

            if sponsor_enabled(self.config) and RemotePlacementClient.from_config(self.config) is not None:
                from .overlay.lock import billing_authority_lock

                billing_lock = billing_authority_lock()
                self._credit0 = not billing_lock.acquire()
        try:
            return self._dispatch(command, tool)
        finally:
            if billing_lock is not None:
                billing_lock.release()

    def _dispatch(self, command: Sequence[str], tool: str) -> RunReceipt:
        if _is_windows():
            # On Windows a full-screen agent TUI clobbers an overlay, so pin the
            # ad via a ConPTY compositor. Other tools / non-interactive runs keep
            # the passthrough path. If pywinpty is missing, degrade gracefully.
            if interactive_terminal() and tool in FULLSCREEN_TUI_TOOLS and _winpty_available():
                return self._run_windows_pty(command, tool)
            return self._run_passthrough(command, tool)

        if interactive_terminal():
            return self._run_posix_pty(command, tool)
        return self._run_passthrough(command, tool)

    def _run_passthrough(self, command: Sequence[str], tool: str) -> RunReceipt:
        start = time.monotonic()
        terminal = interactive_terminal()
        resolved_command = resolve_command(command)
        idle_seconds = configured_idle_seconds(self.config)
        session = None
        # A full-screen agent TUI repaints the bottom row every frame and would
        # erase a pinned overlay, so for those tools surface the sponsor inline at
        # launch instead. The credit/qualification path is unchanged: the card is
        # the same SponsorSession card, just printed once rather than overlaid.
        inline_tool = terminal and tool in FULLSCREEN_TUI_TOOLS
        with StatusRenderer(enabled=terminal) as status:
            if inline_tool:
                # Surface the sponsor BEFORE the agent paints its UI. A one-shot
                # line printed after the TUI has taken over the screen is wiped by
                # its first repaint, so fetch and print here, then launch.
                session = self._sponsor_session(tool)
                card = session.maybe_card(
                    time.monotonic(),
                    idle_for=idle_seconds,
                    terminal_is_interactive=terminal,
                )
                if card:
                    status.note(card.footer(width=status.width))
                    logger.info("sponsor card surfaced inline tool=%s sponsor=%s", tool, card.sponsor)
                else:
                    logger.info("no sponsor placement available (inline) tool=%s", tool)

            try:
                process = subprocess.Popen(resolved_command)
            except FileNotFoundError:
                print(command_not_found_message(command[0]), file=sys.stderr)
                return RunReceipt(127, 0.0, 0, 0.0, self._wallet().balance(), "")

            # Without a ConPTY the child's output cannot be observed here, so
            # "idle" approximates time since the run started or the last card.
            idle_anchor = start
            interrupted = False
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    idle_for = now - idle_anchor
                    if not inline_tool:
                        if session is None and idle_for >= idle_seconds:
                            session = self._sponsor_session(tool)
                        card = None if session is None else session.maybe_card(
                            now,
                            idle_for=idle_for,
                            terminal_is_interactive=terminal,
                        )
                        if card:
                            status.show(card.footer(width=status.width))
                            idle_anchor = now
                    time.sleep(0.2)
            except KeyboardInterrupt:
                interrupted = True
                terminate_process(process)

            exit_code = 130 if interrupted else normalize_exit_code(process.returncode)
            if session is None:
                return self._empty_receipt(start, exit_code)
            receipt = self._settled_receipt(start, session, exit_code)
            if terminal and (receipt.credits_earned or session.qualified_waits):
                status.print_receipt(receipt.render())
            return receipt

    def _run_posix_pty(self, command: Sequence[str], tool: str) -> RunReceipt:
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios
        import tty

        from .compositor import (
            StreamRewriter,
            clamp_line,
            clear_row,
            paint_row,
            park_cursor,
            release_region,
            reserve_region,
        )

        start = time.monotonic()
        last_output = start
        terminal = True
        master_fd = None
        old_stdin = termios.tcgetattr(sys.stdin.fileno())
        old_winch = None
        idle_seconds = configured_idle_seconds(self.config)
        session = None

        # Full-screen agent TUIs clobber an overlay, so pin the card on the
        # bottom row via the same height-fake + stream-rewrite compositor used on
        # Windows. Other tools keep the existing wait-overlay (StatusRenderer).
        pinned = tool in FULLSCREEN_TUI_TOOLS
        rw = StreamRewriter() if pinned else None
        pin = {"card": None, "rows": 0, "cols": 0, "last_paint": 0.0}

        def term_size() -> tuple[int, int]:
            try:
                s = os.get_terminal_size(sys.stdout.fileno())
                return s.columns, s.lines
            except OSError:
                return 80, 24

        def paint(force: bool = False) -> None:
            if not pinned or pin["card"] is None or not rw.safe_to_paint():
                return
            now = time.monotonic()
            if not force and now - pin["last_paint"] < 0.25:
                return
            cols = pin["cols"]  # re-fit to the live width so resize never wraps
            line = clamp_line(pin["card"].footer(width=max(1, cols - 1)), cols)
            os.write(sys.stdout.fileno(), paint_row(pin["rows"], line))
            pin["last_paint"] = now

        def child_setup() -> None:
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        def propagate_winsize() -> None:
            if master_fd is None:
                return
            try:
                cols, rows = term_size()
                child_rows = rows - 1 if pinned else rows  # reserve the bottom row
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", child_rows, cols, 0, 0))
                if pinned:
                    pin["rows"], pin["cols"] = rows, cols
                    rw.set_region_bottom(rows - 1)
                    os.write(sys.stdout.fileno(), reserve_region(rows - 1))
                    paint(force=True)
            except OSError:
                pass

        with StatusRenderer(enabled=True) as status:
            try:
                master_fd, slave_fd = pty.openpty()
                resolved_command = resolve_command(command)
                if pinned:
                    cols, rows = term_size()
                    pin["rows"], pin["cols"] = rows, cols
                    rw.set_region_bottom(rows - 1)
                    # Size the pty to H-1 and park the cursor BEFORE the child
                    # starts, so its first CPR anchors its viewport at <= H-1.
                    # Reserve the region FIRST: DECSTBM homes the cursor to (1,1)
                    # as a side effect, so parking must come after it or the
                    # child starts at the top and overdraws on its first frame.
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows - 1, cols, 0, 0))
                    os.write(sys.stdout.fileno(), reserve_region(rows - 1))
                    os.write(sys.stdout.fileno(), park_cursor(rows - 1))
                process = subprocess.Popen(
                    resolved_command,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                    preexec_fn=child_setup,
                )
                os.close(slave_fd)
            except FileNotFoundError:
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                print(command_not_found_message(command[0]), file=sys.stderr)
                return RunReceipt(127, 0.0, 0, 0.0, self._wallet().balance(), "")

            if not pinned:
                propagate_winsize()
            try:
                old_winch = signal.signal(signal.SIGWINCH, lambda *_: propagate_winsize())
            except ValueError:
                old_winch = None  # not in the main thread

            stdin_fd = sys.stdin.fileno()
            stdin_open = True
            interrupted = False

            def consume_child(data: bytes, when: float) -> None:
                # Billing window for the current card ends when output resumes.
                if session is not None:
                    session.mark_cards_hidden(when)
                if pinned:
                    os.write(sys.stdout.fileno(), rw.feed(data))
                    if rw.repaint_due:
                        rw.repaint_due = False
                        paint(force=True)
                else:
                    status.clear()
                    os.write(sys.stdout.fileno(), data)

            try:
                tty.setraw(stdin_fd)
                while process.poll() is None:
                    fds = [master_fd, stdin_fd] if stdin_open else [master_fd]
                    readable, _, _ = select.select(fds, [], [], 0.2)
                    now = time.monotonic()
                    if master_fd in readable:
                        try:
                            data = os.read(master_fd, 4096)
                        except OSError:
                            data = b""
                        if data:
                            consume_child(data, now)
                            last_output = now
                    if stdin_open and stdin_fd in readable:
                        data = os.read(stdin_fd, 4096)
                        if data:
                            # A keypress means the user is present: reset the AFK
                            # guard so the carousel can keep rotating.
                            if session is not None:
                                session.note_user_input()
                            os.write(master_fd, data)
                        else:
                            # EOF: stop selecting stdin to avoid a busy loop.
                            stdin_open = False

                    idle_for = now - last_output
                    if session is None and idle_for >= idle_seconds:
                        session = self._sponsor_session(tool)
                    card = None if session is None else session.maybe_card(
                        now,
                        idle_for=idle_for,
                        terminal_is_interactive=terminal,
                    )
                    if card:
                        if pinned:
                            pin["card"] = card
                            paint(force=True)
                        else:
                            status.show(card.footer(width=status.width))
                    elif pinned:
                        paint()  # debounced idle re-assert

                # Drain whatever the child wrote between the last poll and exit.
                while True:
                    readable, _, _ = select.select([master_fd], [], [], 0.05)
                    if master_fd not in readable:
                        break
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    consume_child(data, time.monotonic())
            except KeyboardInterrupt:
                interrupted = True
                terminate_process(process)
            finally:
                if pinned:
                    try:
                        os.write(sys.stdout.fileno(), release_region())
                        os.write(sys.stdout.fileno(), clear_row(pin["rows"]))
                    except OSError:
                        pass
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_stdin)
                if old_winch is not None:
                    signal.signal(signal.SIGWINCH, old_winch)
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass

            exit_code = 130 if interrupted else normalize_exit_code(process.wait())
            if session is None:
                return self._empty_receipt(start, exit_code)
            receipt = self._settled_receipt(start, session, exit_code)
            if receipt.credits_earned or session.qualified_waits:
                status.print_receipt(receipt.render())
            return receipt

    def _run_windows_pty(self, command: Sequence[str], tool: str) -> RunReceipt:
        """Windows ConPTY compositor: run the agent in a pseudo-console, report
        the height as H-1 so it stays off the bottom row, rewrite its stream so
        it can never reach row H, and pin the sponsor card there. Billing mirrors
        the passthrough/posix paths (qualified-wait windows); the pin is display."""
        import ctypes
        from ctypes import wintypes
        import threading

        import winpty

        from .compositor import (
            StreamRewriter,
            clamp_line,
            clear_row,
            paint_row,
            park_cursor,
            release_region,
            reserve_region,
        )

        start = time.monotonic()
        idle_seconds = configured_idle_seconds(self.config)
        session = None
        interrupted = False
        resolved_command = resolve_command(command)

        def _size() -> tuple[int, int]:
            try:
                s = os.get_terminal_size()
                return s.columns, s.lines
            except OSError:
                return 80, 24

        # ConPTY spawn first; if it fails for any reason, fall back cleanly
        # (no console state has been touched yet).
        cols, rows = _size()
        try:
            proc = winpty.PtyProcess.spawn(resolved_command, dimensions=(rows - 1, cols))
        except FileNotFoundError:
            print(command_not_found_message(command[0]), file=sys.stderr)
            return RunReceipt(127, 0.0, 0, 0.0, self._wallet().balance(), "")
        except Exception:  # noqa: BLE001 - ConPTY init varies by machine; degrade
            logger.info("ConPTY spawn failed; falling back to passthrough tool=%s", tool)
            return self._run_passthrough(command, tool)

        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetConsoleMode.restype = wintypes.BOOL
        k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.WriteFile.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.ReadFile.restype = wintypes.BOOL
        k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k32.CancelIoEx.restype = wintypes.BOOL
        for fn in (k32.GetConsoleCP, k32.GetConsoleOutputCP):
            fn.restype = wintypes.UINT
        for fn in (k32.SetConsoleCP, k32.SetConsoleOutputCP):
            fn.argtypes = [wintypes.UINT]
            fn.restype = wintypes.BOOL

        EPO, EVTP, EVTI, EXTENDED_FLAGS = 0x0001, 0x0004, 0x0200, 0x0080
        h_in = k32.GetStdHandle(-10)
        h_out = k32.GetStdHandle(-11)

        def get_mode(h):
            m = wintypes.DWORD()
            k32.GetConsoleMode(h, ctypes.byref(m))
            return m.value

        old_in_mode, old_out_mode = get_mode(h_in), get_mode(h_out)
        old_in_cp, old_out_cp = k32.GetConsoleCP(), k32.GetConsoleOutputCP()

        def write_out(data: bytes) -> None:
            written = wintypes.DWORD()
            k32.WriteFile(h_out, data, len(data), ctypes.byref(written), None)

        def read_in(n: int) -> bytes:
            buf = ctypes.create_string_buffer(n)
            nread = wintypes.DWORD()
            ok = k32.ReadFile(h_in, buf, n, ctypes.byref(nread), None)
            if not ok or nread.value == 0:
                return b""
            return buf.raw[: nread.value]

        rw = StreamRewriter()
        rw.set_region_bottom(rows - 1)
        lock = threading.Lock()
        st = {"rows": rows, "cols": cols, "last_paint": 0.0,
              "last_output": start, "alive": True, "card": None, "user_input": False}

        def paint(force: bool = False) -> None:
            with lock:
                card = st["card"]
                if card is None:
                    return
                if not rw.safe_to_paint():
                    rw.repaint_due = True
                    return
                now = time.monotonic()
                if not force and now - st["last_paint"] < 0.25:
                    return
                cols = st["cols"]  # re-fit to the live width so resize never wraps
                line = clamp_line(card.footer(width=max(1, cols - 1)), cols)
                write_out(paint_row(st["rows"], line))
                st["last_paint"] = now

        def reader() -> None:
            while st["alive"]:
                try:
                    data = proc.read(8192)
                except EOFError:
                    break
                except Exception:  # noqa: BLE001
                    break
                if not data:
                    if not proc.isalive():
                        break
                    continue
                raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
                safe = rw.feed(raw)
                with lock:
                    write_out(safe)
                    st["last_output"] = time.monotonic()
                if rw.repaint_due:
                    rw.repaint_due = False
                    paint(force=True)
            st["alive"] = False

        def writer() -> None:
            while st["alive"]:
                data = read_in(4096)
                if not data:
                    if not st["alive"]:
                        break
                    continue
                # A keypress means the user is present: flag it for the main loop
                # to reset the AFK guard (the session lives on that thread).
                with lock:
                    st["user_input"] = True
                try:
                    proc.write(data.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    break

        rt = threading.Thread(target=reader, daemon=True)
        wt = threading.Thread(target=writer, daemon=True)
        try:
            k32.SetConsoleMode(h_out, old_out_mode | EVTP | EPO)
            k32.SetConsoleMode(h_in, EVTI | EXTENDED_FLAGS)  # raw VT in, QuickEdit off
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
            with lock:
                # Reserve FIRST: DECSTBM homes the cursor to (1,1), so park after
                # it — otherwise the child starts at the top and overdraws frame 1.
                write_out(reserve_region(rows - 1))
                write_out(park_cursor(rows - 1))   # final cursor at H-1, anchors CPR <= H-1
            rt.start()
            wt.start()
            prev_seen = st["last_output"]
            while proc.isalive() and st["alive"]:
                time.sleep(0.2)
                now = time.monotonic()

                c, r = _size()
                with lock:
                    resized = (c, r) != (st["cols"], st["rows"])
                    if resized:
                        st["cols"], st["rows"] = c, r
                        rw.set_region_bottom(r - 1)
                if resized:
                    try:
                        proc.setwinsize(r - 1, c)
                    except (AttributeError, OSError) as exc:
                        logger.debug("resize propagate failed: %r", exc)
                    with lock:
                        write_out(reserve_region(r - 1))
                    paint(force=True)

                # Billing: a card's qualified-wait window ends when output resumes.
                last_output = st["last_output"]
                if last_output > prev_seen:
                    if session is not None:
                        session.mark_cards_hidden(last_output)
                    prev_seen = last_output

                with lock:
                    typed = st["user_input"]
                    st["user_input"] = False
                if typed and session is not None:
                    session.note_user_input()

                idle_for = now - st["last_output"]
                if session is None and idle_for >= idle_seconds:
                    session = self._sponsor_session(tool)
                card = None if session is None else session.maybe_card(
                    now, idle_for=idle_for, terminal_is_interactive=True
                )
                if card:
                    with lock:
                        st["card"] = card
                    paint(force=True)
                    logger.info("sponsor card pinned tool=%s sponsor=%s", tool, card.sponsor)
                else:
                    paint()
        except KeyboardInterrupt:
            interrupted = True
        finally:
            st["alive"] = False
            try:
                proc.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
            k32.CancelIoEx(h_in, None)
            rt.join(0.5)
            wt.join(0.5)
            with lock:
                write_out(release_region())
                write_out(clear_row(st["rows"]))
                k32.SetConsoleMode(h_in, old_in_mode)
                k32.SetConsoleMode(h_out, old_out_mode)
                k32.SetConsoleOutputCP(old_out_cp)
                k32.SetConsoleCP(old_in_cp)

        if interrupted:
            exit_code = 130
        else:
            try:
                raw_code = proc.wait()
            except Exception:  # noqa: BLE001
                raw_code = getattr(proc, "exitstatus", None)
            exit_code = normalize_exit_code(raw_code)

        if session is None:
            return self._empty_receipt(start, exit_code)
        receipt = self._settled_receipt(start, session, exit_code)
        if receipt.credits_earned or session.qualified_waits:
            sys.stdout.write(receipt.render() + "\n")
            sys.stdout.flush()
        return receipt

    def _settled_receipt(self, start: float, session: SponsorSession, exit_code: int) -> RunReceipt:
        duration = time.monotonic() - start
        try:
            earned = session.settle()
        except KeyboardInterrupt:
            earned = 0.0
            exit_code = 130
        return RunReceipt(
            exit_code=exit_code,
            duration_seconds=duration,
            qualified_waits=session.qualified_waits,
            credits_earned=earned,
            balance=self._wallet().balance(),
            session_id=session.id,
        )


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=POSIX_INTERRUPT_GRACE_SECONDS)
        else:
            # Ctrl+C has already been delivered to the console process group.
            # Give interactive CLIs a brief chance to restore their terminal
            # state, then fall back to a tree kill for cmd/node wrappers.
            process.wait(timeout=WINDOWS_INTERRUPT_GRACE_SECONDS)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
        pass
    if os.name == "nt" and process.poll() is None and _kill_windows_process_tree(process.pid):
        try:
            process.wait(timeout=WINDOWS_KILL_WAIT_SECONDS)
            return
        except (subprocess.TimeoutExpired, OSError):
            pass
    if process.poll() is None:
        process.kill()
        process.wait()


def _kill_windows_process_tree(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=WINDOWS_KILL_WAIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _winpty_available() -> bool:
    try:
        import winpty  # noqa: F401
    except Exception:  # noqa: BLE001 - missing or broken native wheel
        return False
    return True


def normalize_exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 0
    code = int(returncode)
    if code < 0:
        return 128 + abs(code)
    return code


def resolve_command(command: Sequence[str]) -> list[str]:
    if not command:
        raise ValueError("No command provided")

    executable = command[0]
    if any(sep in executable for sep in {os.sep, os.altsep} if sep):
        return list(command)

    found = shutil.which(executable)
    if found:
        return [found, *command[1:]]

    if os.name == "nt":
        fallback = resolve_windows_fallback(executable)
        if fallback:
            return [*fallback, *command[1:]]

    return list(command)


def resolve_windows_fallback(executable: str) -> list[str] | None:
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    candidate_dirs = []
    if appdata:
        candidate_dirs.append(os.path.join(appdata, "npm"))
    if localappdata:
        candidate_dirs.append(os.path.join(localappdata, "Programs", "OpenAI", "Codex", "bin"))

    for directory in candidate_dirs:
        for extension in (".cmd", ".exe", ".bat", ".ps1", ""):
            candidate = os.path.join(directory, executable + extension)
            if os.path.exists(candidate):
                if extension == ".ps1":
                    return [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        candidate,
                    ]
                return [candidate]
    return None


def command_not_found_message(executable: str) -> str:
    hints = []
    if os.name == "nt":
        hints.append("On Windows, check: Get-Command " + executable)
        hints.append("Common npm path: %APPDATA%\\npm")
    hint_text = "\n".join(hints)
    if hint_text:
        return f"sai: command not found: {executable}\n{hint_text}"
    return f"sai: command not found: {executable}"
