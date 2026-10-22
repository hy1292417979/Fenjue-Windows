"""开关 Codex 重试代理：python control.py on|off|status

on   启动代理 + 开机自启 + 把 Codex 的 model_provider 指向代理
off  把 Codex 改回 Cockpit + 停止代理 + 取消开机自启
只改 config.toml 顶部的 model_provider 一行，并追加一个 [model_providers.codex_retry] 段；
Cockpit 自己的 provider 段（地址、密钥）一律不碰。改前备份 config.toml.bak_retry_proxy。
"""
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from proxy import HOST, PORT, OWN, DEFAULT_UPSTREAM, STATE, LOG, VERSION, codex_config  # noqa: E402

HEALTH = f'http://{HOST}:{PORT}/__retry_proxy/health'
SECTION = (f'\n[model_providers.{OWN}]\n'
           'name = "Codex 重试代理 -> Cockpit"\n'
           f'base_url = "http://{HOST}:{PORT}/v1"\n'
           'wire_api = "responses"\n'
           'requires_openai_auth = false\n'
           'supports_websockets = false\n')
SHORTCUT_NAME = 'Codex重试代理.lnk'


def read_raw(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return f.read()


def say(text):
    print(text, flush=True)


# ---------- config.toml ----------

def current_provider(text):
    return tomllib.loads(text).get('model_provider')


def set_provider(text, name):
    """Replace the top-level model_provider line (before the first [table])."""
    lines = text.splitlines(keepends=True)
    newline = '\r\n' if '\r\n' in text else '\n'
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('['):
            break
        if re.match(r'model_provider\s*=', stripped):
            lines[i] = f'model_provider = "{name}"' + newline
            return ''.join(lines)
    return f'model_provider = "{name}"' + newline + text


def ensure_section(text):
    if re.search(rf'^\[model_providers\.{OWN}\]\s*$', text, re.M):
        return text
    newline = '\r\n' if '\r\n' in text else '\n'
    return text.rstrip('\r\n') + newline + SECTION.replace('\n', newline)


def write_config(path, old_text, new_text):
    parsed = tomllib.loads(new_text)  # never write an invalid file
    if read_raw(path) != old_text:
        raise RuntimeError('config.toml 刚被别的程序改过，请完全退出 Codex 后重试。')
    tmp = path.with_name(path.name + '.retry_proxy.tmp')
    tmp.write_text(new_text, encoding='utf-8', newline='')
    os.replace(tmp, path)
    return parsed


def point_codex_to_proxy(path):
    text = read_raw(path)
    cfg = tomllib.loads(text)
    previous = cfg.get('model_provider')
    if previous != OWN:
        upstream = previous or DEFAULT_UPSTREAM
        if upstream not in (cfg.get('model_providers') or {}):
            raise RuntimeError(f'当前 Codex 用的 provider [{upstream}] 不在 config.toml 里，无法接到 Cockpit。')
        STATE.write_text(json.dumps({'upstream': upstream}), encoding='utf-8')
        path.with_name(path.name + '.bak_retry_proxy').write_text(text, encoding='utf-8', newline='')
    new_text = set_provider(ensure_section(text), OWN)
    if new_text != text:
        write_config(path, text, new_text)
    return previous


def point_codex_to_cockpit(path):
    text = read_raw(path)
    if current_provider(text) != OWN:
        return False
    try:
        upstream = json.loads(STATE.read_text(encoding='utf-8')).get('upstream') or DEFAULT_UPSTREAM
    except (OSError, ValueError):
        upstream = DEFAULT_UPSTREAM
    write_config(path, text, set_provider(text, upstream))
    return True


# ---------- process ----------

def health():
    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            value = client.get(HEALTH).json()
        return value if value.get('service') == 'codex-retry-proxy' else None
    except Exception:  # noqa: BLE001
        return None


def pythonw():
    exe = Path(sys.executable)
    candidate = exe.with_name('pythonw.exe')
    return candidate if candidate.exists() else exe


def start_proxy():
    value = health()
    if value and value.get('version') == VERSION:
        return value
    if value:
        stop_proxy(value)
    flags = 0
    if os.name == 'nt':
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([str(pythonw()), str(HERE / 'proxy.py')], cwd=str(HERE), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    for _ in range(40):
        time.sleep(0.25)
        value = health()
        if value:
            return value
    raise RuntimeError(f'代理没能启动，查看 {HERE / "stderr.log"}；端口 {PORT} 可能被占用。')


def stop_proxy(value=None):
    value = value or health()
    if not value:
        return False
    pid = int(value['pid'])
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.kill(pid, 15)
    for _ in range(20):
        time.sleep(0.25)
        if not health():
            return True
    return False


# ---------- autostart (Startup folder shortcut, no admin needed) ----------

def startup_dir():
    return Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming'))) / 'Microsoft/Windows/Start Menu/Programs/Startup'


def set_autostart(enabled):
    link = startup_dir() / SHORTCUT_NAME
    if not enabled:
        if link.exists():
            link.unlink()
        return
    if os.name != 'nt':
        return
    env = dict(os.environ, RP_LINK=str(link), RP_TARGET=str(pythonw()),
               RP_ARGS=f'"{HERE / "proxy.py"}"', RP_DIR=str(HERE))
    script = ('$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:RP_LINK);'
              '$s.TargetPath=$env:RP_TARGET;$s.Arguments=$env:RP_ARGS;$s.WorkingDirectory=$env:RP_DIR;'
              '$s.WindowStyle=7;$s.Description="Codex 重试代理";$s.Save()')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], env=env, check=True,
                   stdout=subprocess.DEVNULL)


# ---------- commands ----------

def cmd_on():
    path = codex_config()
    value = start_proxy()
    say(f'√ 代理已运行（PID {value["pid"]}），上游：{value["upstream_base"]}')
    set_autostart(True)
    say('√ 已设为开机自启')
    previous = point_codex_to_proxy(path)
    value = start_proxy()  # state.json may have changed the upstream; health re-reads it
    if previous == OWN:
        say('√ Codex 本来就指向代理')
    else:
        say(f'√ Codex 已改为：Codex → 代理 → Cockpit（原来的 provider：{previous}）')
        say('')
        say('请【完全退出 Codex 再重新打开】，新开的任务才会经过代理。')
    if str(value.get('upstream_base', '')).startswith('error'):
        say('! 代理读不到 Cockpit 地址：' + value['upstream_base'])
        return 1
    return 0


def cmd_off():
    changed = point_codex_to_cockpit(codex_config())
    say('√ Codex 已改回直连 Cockpit' if changed else '√ Codex 本来就没走代理')
    stopped = stop_proxy()
    say('√ 代理已停止' if stopped else '√ 代理本来就没在运行')
    set_autostart(False)
    say('√ 已取消开机自启')
    if changed:
        say('')
        say('请完全退出 Codex 再重新打开。')
    return 0


def cmd_status():
    value = health()
    path = codex_config()
    provider = current_provider(read_raw(path))
    say(f'代理进程：{"运行中，PID " + str(value["pid"]) if value else "没有运行"}')
    say(f'开机自启：{"已开启" if (startup_dir() / SHORTCUT_NAME).exists() else "未开启"}')
    say(f'Codex 当前 provider：{provider}' + ('（经过代理）' if provider == OWN else '（不经过代理）'))
    if value:
        say(f'上游 Cockpit：{value["upstream_base"]}（provider：{value["upstream_provider"]}）')
        say(f'本次运行以来：请求 {value["requests"]}，遇到忙 {value["retried"]}，'
            f'重试后成功 {value["recovered"]}，重试用尽 {value["gave_up"]}')
    if provider == OWN and not value:
        say('! Codex 指向代理但代理没运行：Codex 会连不上。请选“开启”或“关闭”。')
    elif provider != OWN and value:
        say('! 代理在运行，但 Codex 没走代理（可能被 Cockpit 改回了）。需要的话再选一次“开启”。')
    if LOG.exists():
        lines = LOG.read_text(encoding='utf-8', errors='replace').splitlines()[-5:]
        if lines:
            say('')
            say('最近的重试记录：')
            for line in lines:
                say('  ' + line)
    return 0


def main(argv):
    commands = {'on': cmd_on, 'off': cmd_off, 'status': cmd_status}
    if len(argv) != 2 or argv[1] not in commands:
        say('用法：control.py on|off|status')
        return 2
    try:
        return commands[argv[1]]()
    except Exception as exc:  # noqa: BLE001 - shown to the user in the menu
        say(f'× 出错：{exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
