"""Guard hardening helpers: evasion-resistant command analysis (pure, no I/O).

The base guard in ``__init__.py`` matches dangerous *shapes* (``rm -rf``,
``git push --force``, ``.env``) at command position. Real shells offer many
ways to hide the same command, and the 0.3.0-dev audit (R13) showed three
that slipped through: absolute binary paths (``/bin/rm``), wrapped shells
(``bash -c "…"``) and decode-and-pipe (``echo … | base64 -d | bash``).

This module does NOT try to be a shell parser. It produces a bounded list of
*variants* of the command text — the original, a normalised form (path
prefixes, alias backslashes, quote splicing, wrapper words stripped) and every
nested payload it can recover (``-c`` bodies, ``eval``, ``$(…)``, backticks,
PowerShell ``-EncodedCommand``, inline base64) — and the caller runs the
existing rules over each variant. Plus three extra detectors:

* ``obfuscated_exec``  decoder piped into a shell/interpreter;
* ``network_exfil``    a local file/secret leaving through curl/wget/nc/scp…;
* ``memory_injection`` instruction-like text written into agent memory files.

It is a layer against accidents and copy-pasted tricks, **not a sandbox**: a
determined user can still obfuscate. All regexes are linear and every input
is length-capped, so a hostile command cannot stall the hook.
"""
from __future__ import annotations

import base64
import binascii
import re
import shlex
from typing import Any

try:
    from untrusted import find_injection
except Exception:  # pragma: no cover - sibling missing
    def find_injection(value):  # type: ignore[misc]
        return []

MAX_CMD_CHARS = 20000
MAX_VARIANTS = 24
MAX_DEPTH = 3

_BINARIES = (
    "rm|rmdir|rd|unlink|shred|git|find|del|erase|rimraf|curl|wget|nc|ncat|netcat|socat|scp|sftp|"
    "rsync|ftp|bash|sh|zsh|dash|ksh|ash|fish|csh|tcsh|python3?(?:\\.\\d+)?|perl|ruby|node|php|"
    "powershell|pwsh|cmd|base64|openssl|xxd|busybox|sudo|doas|xargs|env|printenv|eval|tee|dd|mv|cp"
)
_ABS_BIN_RE = re.compile(
    r"(?<![\w.$\\/-])(?:[A-Za-z]:)?[\\/]+(?:[\w.+~@-]+[\\/]+)*(?=(?:" + _BINARIES +
    r")(?:\.exe)?(?![\w.-]))", re.I)
_WRAPPERS = ("sudo|doas|env|command|exec|nohup|time|nice|ionice|stdbuf|busybox|builtin|setsid")
_WRAPPER_RE = re.compile(
    r"((?:^|[;&|(\n])\s*)(?:(?:" + _WRAPPERS + r")\b(?:\s+-{1,2}[\w=-]+(?:\s+\d+)?)*"
    r"(?:\s+[A-Za-z_]\w*=\S*)*\s+)+", re.I)
_ALIAS_BS_RE = re.compile(r"(?<![\w\\:])\\(?=[A-Za-z])")
_EMPTY_QUOTES_RE = re.compile(r"(?<=\w)(?:''|\"\")(?=\w)")
_QUOTED_WORD_RE = re.compile(r"((?:^|[;&|(\n])\s*)([\"'])([\w.+-]+)\2(?=\s|$|[;&|)])")
_IFS_RE = re.compile(r"\$\{IFS\}|\$IFS")

# <shell-ish> [up to 4 flags] <-c style flag> <payload…>
_SHELLS = r"(?:(?:ba|z|da|k|a|c|fi|tc)?sh|cmd|powershell|pwsh)"
_C_FLAG = r"(?:-[a-z]*c[a-z]*|/[ck]|-command|-encodedcommand|-enc|-ec|-e|--command)"
_SHELL_C_RE = re.compile(
    r"(?<![\w.-])" + _SHELLS + r"(?:\.exe)?(?:\s+-{1,2}[\w-]+){0,4}\s+" + _C_FLAG + r"(?=\s|$|[\"'])\s*",
    re.I)
_INTERP_C_RE = re.compile(
    r"(?<![\w.-])(?:python[\d.]*|perl|ruby|node|php)(?:\.exe)?(?:\s+-{1,2}[\w-]+){0,3}\s+"
    r"(?:-c|-e|-r|--eval|-E)(?=\s|$|[\"'])\s*", re.I)
_EVAL_RE = re.compile(r"(?<![\w.-])(?:eval|source|exec)\s+(?=[\"'$`(])", re.I)
_SUBST_RE = re.compile(r"\$\(([^()]{1,2000})\)|`([^`]{1,2000})`")
_STRING_LITERAL_RE = re.compile(r"\"((?:\\.|[^\"\\]){2,2000})\"|'((?:\\.|[^'\\]){2,2000})'")
_B64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{8,2000}={0,2}(?![A-Za-z0-9+/=])")
_B64_HINT_RE = re.compile(r"base64|FromBase64String|certutil|openssl\s+(?:enc|base64)|-enc(?:odedcommand)?\b", re.I)
_ENC_CMD_RE = re.compile(r"-(?:e|ec|enc|encodedcommand)\s+([A-Za-z0-9+/]{16,}={0,2})(?![A-Za-z0-9+/=])", re.I)


def _printable(text: str) -> bool:
    if not text or len(text) > 4000:
        return False
    good = sum(1 for ch in text if ch.isprintable() or ch in "\n\t\r")
    return good / len(text) > 0.95


def _try_b64(token: str) -> list[str]:
    out: list[str] = []
    pad = token + "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(pad, validate=False)
    except (binascii.Error, ValueError):
        return out
    for enc in ("utf-8", "utf-16-le"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "utf-16-le" and any(ord(ch) > 0x24F for ch in text):
            continue
        if _printable(text) and len(text.strip()) >= 3:
            out.append(text)
    return out


def normalize(cmd: str) -> str:
    """Form of ``cmd`` with the usual cosmetic evasions undone."""
    text = _IFS_RE.sub(" ", cmd)
    text = _ABS_BIN_RE.sub("", text)
    text = _EMPTY_QUOTES_RE.sub("", text)
    text = _QUOTED_WORD_RE.sub(lambda m: m.group(1) + m.group(3), text)
    text = _ALIAS_BS_RE.sub("", text)
    for _ in range(3):
        new = _WRAPPER_RE.sub(lambda m: m.group(1), text)
        if new == text:
            break
        text = new
    return text


MAX_MATCHES = 6
MAX_SHLEX_CHARS = 4000


def _body_after(tail: str) -> str:
    """The command string a ``-c`` style flag hands to the inner shell."""
    tail = tail[:MAX_SHLEX_CHARS].strip()
    if not tail:
        return ""
    if tail[0] in "\"'":
        try:
            parts = shlex.split(tail, posix=True)
            if parts:
                return parts[0]
        except ValueError:
            pass
        return tail.strip("\"'`")
    return tail


def _nested(text: str) -> list[str]:
    out: list[str] = []
    for rx in (_SHELL_C_RE, _INTERP_C_RE):
        for i, m in enumerate(rx.finditer(text)):
            if i >= MAX_MATCHES:
                break
            body = _body_after(text[m.end():])
            if body:
                out.append(body)
                if rx is _INTERP_C_RE:  # python -c "<code>": string literals inside the code are commands
                    for lit in _STRING_LITERAL_RE.finditer(body):
                        out.append(lit.group(1) or lit.group(2) or "")
    for i, m in enumerate(_EVAL_RE.finditer(text)):
        if i >= MAX_MATCHES:
            break
        body = _body_after(text[m.end():])
        if body:
            out.append(body)
    for i, m in enumerate(_SUBST_RE.finditer(text)):
        if i >= MAX_MATCHES * 2:
            break
        out.append(m.group(1) or m.group(2) or "")
    for i, m in enumerate(_ENC_CMD_RE.finditer(text)):
        if i >= 3:
            break
        out.extend(_try_b64(m.group(1)))
    if _B64_HINT_RE.search(text):
        n = 0
        for m in _B64_TOKEN_RE.finditer(text):
            out.extend(_try_b64(m.group(0)))
            n += 1
            if n >= 5:
                break
    return [o for o in out if o and o.strip()]


def command_variants(cmd: str) -> list[str]:
    """The command plus its normalised form and every recoverable nested payload."""
    cmd = (cmd or "")[:MAX_CMD_CHARS]
    seen: list[str] = []
    todo: list[tuple[str, int]] = [(cmd, 0)]
    while todo and len(seen) < MAX_VARIANTS:
        text, depth = todo.pop(0)
        if not text.strip():
            continue
        for form in (text, normalize(text)):
            if form not in seen:
                seen.append(form)
        if depth < MAX_DEPTH:
            norm = normalize(text)
            payloads = _nested(text) + (_nested(norm) if norm != text else [])
            for payload in payloads[: MAX_VARIANTS]:
                if payload not in seen:
                    todo.append((payload, depth + 1))
    return seen[:MAX_VARIANTS]


# ----------------------------------------------------------- obfuscated exec
_DECODER_RE = re.compile(
    r"\b(?:base64\s+(?:-d\b|-D\b|--decode)|xxd\s+-r|openssl\s+(?:enc|base64)\b[^|;&\n]*\s-d\b|"
    r"certutil\s+-decode|uudecode|rev|gunzip|zcat|bzcat|xzcat|gzip\s+-d|unxz|tr\s+['\"]?[A-Za-z]-[A-Za-z]"
    r"[^|;&\n]*)(?=[\s|)\"\x27`;&]|$)|printf\s+['\"][^'\"\n]*\\x[0-9a-f]{2}|FromBase64String", re.I)
_SHELLISH = (r"(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+)?(?:[\\/\w.~-]*[\\/])?"
             r"(?:(?:ba|z|da|k|a|c|fi|tc)?sh|python[\d.]*|perl|ruby|node|php|pwsh|powershell|iex|eval|source|"
             r"cmd)(?:\.exe)?(?![\w.-])")
_PIPE_SHELL_RE = re.compile(r"\|\s*" + _SHELLISH, re.I)
_PROC_SUBST_RE = re.compile(r"(?:" + _SHELLISH + r"|\.)\s+<\(", re.I)
_EVAL_SUBST_RE = re.compile(r"\b(?:eval|source|iex|Invoke-Expression)\b[^\n]{0,40}(?:\$\(|`|\()", re.I)
_IEX_RE = re.compile(r"\b(?:iex|Invoke-Expression)\b", re.I)


def obfuscated_exec(cmd: str) -> bool:
    """True when decoded/decompressed data is executed (decoder → shell/interpreter)."""
    text = (cmd or "")[:MAX_CMD_CHARS]
    if not text:
        return False
    m = _DECODER_RE.search(text)
    if not m:
        return False
    tail = text[m.start():]
    if _PIPE_SHELL_RE.search(tail):
        return True
    if _PROC_SUBST_RE.search(text) and _DECODER_RE.search(text):
        return True
    if _EVAL_SUBST_RE.search(text):
        return True
    if _IEX_RE.search(text) and "FromBase64String" in text:
        return True
    return False


# ------------------------------------------------------------ network egress
_NET_TOOLS = r"(?:curl|wget|http|https|xh|httpie|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)"
_UPLOAD_FLAG_RE = re.compile(
    r"(?<![\w-])(?:-d|--data(?:-binary|-raw|-ascii|-urlencode)?|-F|--form(?:-string)?)\s*=?\s*[\"']?"
    r"(?:[\w.-]+=)?(?:@|<)|(?<![\w-])(?:-T|--upload-file|--post-file|--body-file|-InFile)(?=[\s=])", re.I)
_UPLOAD_SUBST_RE = re.compile(
    r"(?:-d|--data\S*|-F|--form\S*|-Body)\s*[=]?\s*[\"']?[^\s|;&]*(?:\$\((?:cat|<|type|Get-Content)|`cat\b|\$\(<)", re.I)
_PIPE_NET_RE = re.compile(r"\|\s*(?:nc|ncat|netcat|socat|telnet|openssl\s+s_client)\b", re.I)
_PIPE_CURL_RE = re.compile(
    r"\|\s*(?:curl|wget)\b[^|;\n]*(?:-d\b|--data|-T\b|--upload-file|--post-(?:data|file)|-F\b|--form)", re.I)
_DEVTCP_RE = re.compile(r"/dev/(?:tcp|udp)/", re.I)
_REMOTE_COPY_RE = re.compile(
    r"(?:^|[;&|(\n]\s*)(?:scp|rsync|sftp)\b[^\n;&|]*\s\S+@\S+:", re.I)
_URL_RE = re.compile(r"\b(?:https?|ftp)://([^/\s\"'?#:]+)", re.I)
_LOOPBACK = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"}


def network_exfil(cmd: str) -> bool:
    """A local file/secret leaves the machine through a network tool."""
    text = (cmd or "")[:MAX_CMD_CHARS]
    if not text:
        return False
    hosts = [h.lower() for h in _URL_RE.findall(text)]
    loopback_only = bool(hosts) and all(h in _LOOPBACK or h.endswith(".localhost") for h in hosts)
    if _PIPE_NET_RE.search(text) or _DEVTCP_RE.search(text):
        return True
    if _REMOTE_COPY_RE.search(text):
        return True
    if loopback_only:
        return False
    if re.search(_NET_TOOLS, text, re.I):
        if _UPLOAD_FLAG_RE.search(text) or _UPLOAD_SUBST_RE.search(text):
            return True
    if _PIPE_CURL_RE.search(text):
        return True
    return False


# ------------------------------------------------------------ memory poison
_MEMORY_PATH_RE = re.compile(
    r"(?:^|[\\/])(?:MEMORY|USER|SOUL|AGENTS|CLAUDE|GEMINI|HERMES)\.md$|(?:^|[\\/])memories?[\\/]|"
    r"\.cursorrules$|(?:^|[\\/])\.(?:hermes|claude|cursor)[\\/](?:rules|instructions|memory)", re.I)
_MEMORY_TOOLS = {"memory", "memory_add", "memory_write", "memory_update", "save_memory", "remember",
                 "add_memory", "update_memory"}
_WRITE_TOOLS = {"write_file", "edit_file", "patch", "apply_patch", "str_replace", "append_file",
                "create_file", "replace_in_file", "str_replace_editor", "edit"}
_STANDING_RE = re.compile(
    r"\b(?:standing\s+rule|permanent\s+rule|regla\s+permanente|from\s+now\s+on|a\s+partir\s+de\s+ahora)\b"
    r"[^\n]{0,80}\b(?:approve|ignore|bypass|skip|disable|allow|aprueba|ignora|omite|desactiva|salta|permite)\b|"
    r"\b(?:always|never|siempre|nunca)\b[^\n]{0,40}\b(?:approve|bypass|skip\s+(?:the\s+)?guard|disable\s+(?:the\s+)?guard|"
    r"ignore\s+(?:the\s+)?(?:guard|rules)|aprueba|ignora\s+(?:la\s+)?(?:guardia|reglas))\b", re.I)
_REDIRECT_MEMORY_RE = re.compile(
    r"(?:>>?|\btee\b(?:\s+-a)?)\s*[\"']?[^\s\"'|;&]*(?:MEMORY|USER|SOUL|AGENTS|CLAUDE|GEMINI)\.md\b", re.I)
_CONTENT_KEYS = ("content", "text", "new_string", "new_str", "new_text", "patch", "diff", "data",
                 "value", "memory", "entry", "note", "insert_text", "replacement", "body")


def _content_of(args: dict) -> str:
    parts = []
    for k in _CONTENT_KEYS:
        v = args.get(k)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)[:MAX_CMD_CHARS]


def memory_injection(tool_name: str, args: Any, cmd: str = "", path: str = "") -> bool:
    """Instruction-like content written into an agent memory / rules file."""
    a = args if isinstance(args, dict) else {}
    name = (tool_name or "").strip().lower()
    content = _content_of(a)
    is_mem_tool = name in _MEMORY_TOOLS
    is_mem_path = bool(path and _MEMORY_PATH_RE.search(path.replace("\\", "/")))
    if (is_mem_tool or (name in _WRITE_TOOLS and is_mem_path)) and content:
        return bool(find_injection(content) or _STANDING_RE.search(content))
    if cmd and _REDIRECT_MEMORY_RE.search(cmd):
        return bool(find_injection(cmd) or _STANDING_RE.search(cmd))
    return False
