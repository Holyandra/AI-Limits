"""Read-only quota providers. Credentials stay in the original applications."""
from __future__ import annotations

import json
import os
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import psutil

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


@dataclass
class Quota:
    group: str
    window: str
    remaining: float | None
    reset: float | None = None
    disabled: bool = False
    description: str = ''


@dataclass
class Snapshot:
    key: str
    title: str
    quotas: list[Quota] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    note: str = ''
    updated: float = 0
    error: str = ''
    plan: str = ''

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data['quotas'] = [Quota(**q) for q in data.get('quotas', [])]
        return cls(**data)


def timestamp(value):
    if value is None or value == '':
        return None
    try:
        if isinstance(value, dict):
            return float(value['seconds'])
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, KeyError):
        return None


def percent(value):
    try:
        import math
        number = float(value)
        return max(0., min(100., number)) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def window_name(minutes):
    if minutes == 10080:
        return 'Неделя'
    if minutes == 300:
        return '5 часов'
    if minutes is None:
        return 'Окно не указано'
    if minutes % 1440 == 0:
        return f'{minutes // 1440} дн.'
    if minutes % 60 == 0:
        return f'{minutes // 60} ч.'
    return f'{minutes} мин.'


def parse_codex(data, models=()):
    result = Snapshot('codex', 'Codex', models=list(models), updated=time.time())
    buckets = data.get('rateLimitsByLimitId') or {}
    if not buckets and data.get('rateLimits'):
        bucket = data['rateLimits']
        buckets = {bucket.get('limitId') or 'codex': bucket}
    for key, bucket in buckets.items():
        label = bucket.get('limitName') or ('Общая квота Codex' if key == 'codex' else key)
        result.plan = bucket.get('planType') or result.plan
        for slot in ('primary', 'secondary'):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            used = percent(window.get('usedPercent'))
            result.quotas.append(Quota(label, window_name(window.get('windowDurationMins')),
                                       None if used is None else 100 - used,
                                       timestamp(window.get('resetsAt'))))
    result.note = 'Сервис возвращает квоты по группам. Отдельные лимиты моделей показаны, если их выдаёт Codex.'
    if not result.quotas:
        raise RuntimeError('Codex не вернул квоты. Проверьте вход через ChatGPT в Codex.')
    return result


def parse_antigravity(key, title, summary, status):
    result = Snapshot(key, title, updated=time.time())
    user = status.get('userStatus') or {}
    result.plan = user.get('planStatus', {}).get('planInfo', {}).get('planName', '')
    configs = user.get('cascadeModelConfigData', {}).get('clientModelConfigs', [])
    result.models = sorted({m['label'] for m in configs if m.get('label') and not m.get('disabled')})
    response = summary.get('response') or {}
    for group in response.get('groups', []):
        label = group.get('displayName') or 'Модели Antigravity'
        for bucket in group.get('buckets', []):
            name = {'5h': '5 часов', 'weekly': 'Неделя'}.get(bucket.get('window'), bucket.get('displayName') or 'Квота')
            # Proto oneof absence is unknown, whereas an explicit 0 means exhausted.
            remaining = percent(bucket['remainingFraction'] * 100) if isinstance(bucket.get('remainingFraction'), (int, float)) else None
            result.quotas.append(Quota(label, name, remaining, timestamp(bucket.get('resetTime')),
                                       bool(bucket.get('disabled')), bucket.get('description', '')))
    if not result.quotas:
        raise RuntimeError('Antigravity не вернула недельные и пятичасовые квоты. Обновите приложение и войдите в аккаунт.')
    result.note = 'Модели внутри группы расходуют общую квоту. Неактивное окно не означает, что модель доступна.'
    return result


def codex_executable():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    # Prefer exactly the desktop version that is currently running.
    for proc in psutil.process_iter(['name', 'exe']):
        try:
            exe = proc.info['exe'] or ''
            if (proc.info['name'] or '').lower() == 'codex.exe' and 'openai' in exe.lower() and Path(exe).is_file():
                return exe
        except (psutil.Error, OSError):
            pass
    paths = list((base / 'OpenAI/Codex/bin').glob('*/codex.exe'))
    roaming = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    paths += list((roaming / 'npm/node_modules/@openai/codex').glob('**/bin/codex.exe'))
    if not paths:
        raise RuntimeError('Не найден codex.exe. Установите или запустите Codex.')
    return str(max(paths, key=lambda p: p.stat().st_mtime))


class CodexProvider:
    def __init__(self):
        self.process = None
        self.responses = queue.Queue()
        self.counter = 0
        self.models = []
        self.models_updated = 0
        self.closed = False
        self.proxy = ''

    def _start(self):
        if self.closed:
            raise RuntimeError('Монитор остановлен')
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        # This environment affects only our child. Never change the system proxy.
        try:
            with socket.create_connection(('127.0.0.1', 10808), timeout=.3):
                pass
            self.proxy = 'http://127.0.0.1:10808'
            env.update(HTTPS_PROXY=self.proxy, HTTP_PROXY=self.proxy, NO_PROXY='localhost,127.0.0.1,::1')
        except OSError:
            self.proxy = ''
        self.responses = queue.Queue()
        process = subprocess.Popen([codex_executable(), 'app-server', '--listen', 'stdio://'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding='utf-8', env=env, creationflags=NO_WINDOW)
        self.process = process
        responses = self.responses
        def read():
            try:
                for line in process.stdout:
                    try:
                        msg = json.loads(line)
                        if 'id' in msg:
                            responses.put(msg)
                    except ValueError:
                        pass
            finally:
                responses.put({'_closed': True})
        threading.Thread(target=read, daemon=True).start()
        self._rpc('initialize', {'clientInfo': {'name': 'quota_widget', 'title': 'Quota Widget', 'version': '1.0.0'}})
        self._send({'method': 'initialized'})

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def _rpc(self, method, params=None):
        self.counter += 1
        id_ = self.counter
        message = {'id': id_, 'method': method}
        if params is not None:
            message['params'] = params
        self._send(message)
        deadline = time.monotonic() + 25
        while True:
            try:
                response = self.responses.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError('Codex не ответил за 25 секунд. Проверьте сеть и прокси.') from None
            if response.get('_closed'):
                raise RuntimeError('Служба Codex завершилась. Повторите обновление.')
            if response.get('id') != id_:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Время ожидания Codex истекло')
                continue
            if response.get('error'):
                # No raw server errors in logs: they may contain account data.
                raise RuntimeError('Не удалось прочитать квоты Codex. Проверьте вход в аккаунт и подключение к сети.')
            return response.get('result') or {}

    def fetch(self):
        try:
            self._start()
            data = self._rpc('account/rateLimits/read')
            if time.time() - self.models_updated > 900:
                try:
                    models, cursor = [], None
                    for _ in range(20):
                        params = {'limit': 100, 'includeHidden': False}
                        if cursor:
                            params['cursor'] = cursor
                        page = self._rpc('model/list', params)
                        models.extend(m.get('displayName') or m.get('model') or m.get('id', '') for m in page.get('data', []))
                        cursor = page.get('nextCursor')
                        if not cursor:
                            break
                    self.models = sorted(set(filter(None, models)))
                    self.models_updated = time.time()
                except RuntimeError:
                    pass
            return parse_codex(data, self.models)
        except Exception:
            self._terminate()
            raise
        finally:
            # Free the app-server's memory between polls. Catalog stays cached here.
            self._terminate()

    def _terminate(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def close(self):
        self.closed = True
        self._terminate()


class AntigravityProvider:
    def __init__(self):
        self.endpoints = {}
        self.scan_time = 0
        self.discovery_errors = {}
        # Self-signed TLS is allowed ONLY for an identified local language server.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))

    def _request(self, base, token, method, body):
        req = urllib.request.Request(base + '/exa.language_server_pb.LanguageServerService/' + method,
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Codeium-Csrf-Token': token})
        with self.opener.open(req, timeout=12) as response:
            return json.load(response)

    def _discover(self):
        found = {}
        for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
            try:
                if not (proc.info['name'] or '').lower().startswith('language_server'):
                    continue
                exe = (proc.info['exe'] or '').lower().replace('\\', '/')
                if '/programs/antigravity/' in exe:
                    key, title = 'antigravity', 'Antigravity 2.0'
                elif '/programs/antigravity ide/' in exe:
                    key, title = 'antigravity-ide', 'Antigravity IDE'
                else:
                    continue
                args = proc.info['cmdline'] or []
                token = None
                for n, arg in enumerate(args):
                    if arg == '--csrf_token' and n + 1 < len(args):
                        token = args[n + 1]
                    elif arg.startswith('--csrf_token='):
                        token = arg.split('=', 1)[1]
                if not token:
                    continue
                ports = sorted({c.laddr.port for c in proc.net_connections(kind='tcp') if c.status == psutil.CONN_LISTEN})
                for port in ports:
                    for scheme in ('https', 'http'):
                        base = f'{scheme}://127.0.0.1:{port}'
                        try:
                            status = self._request(base, token, 'GetUserStatus', {})
                            if status.get('userStatus'):
                                found[key] = (base, token, proc.pid, title)
                                break
                        except (OSError, ValueError):
                            continue
                    if key in found:
                        break
            except (psutil.Error, OSError):
                continue
        self.endpoints = found
        self.scan_time = time.monotonic()

    def fetch(self):
        dead = any(not psutil.pid_exists(e[2]) for e in self.endpoints.values())
        if dead or not self.endpoints or time.monotonic() - self.scan_time > 60:
            self._discover()
        # These installations expose the same account quotas. Prefer the
        # source with the fuller model catalog, falling back to IDE if needed.
        for key in ('antigravity', 'antigravity-ide'):
            endpoint = self.endpoints.get(key)
            if not endpoint:
                continue
            base, token, _pid, _title = endpoint
            try:
                status = self._request(base, token, 'GetUserStatus', {})
                # Same refresh flag the installed Antigravity UI uses on opening quota.
                summary = self._request(base, token, 'RetrieveUserQuotaSummary', {'forceRefresh': True})
                return [parse_antigravity('antigravity', 'Antigravity', summary, status)]
            except Exception:
                self.scan_time = 0
        return [Snapshot('antigravity', 'Antigravity', error='Запустите Antigravity и войдите в аккаунт: квоты читаются через её локальную службу.')]
