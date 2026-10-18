"""
プロキシ回し（proxy.txt から1行1つ読み込む。1プロキシ同時3セッションまで）

proxy.txt（ぷにフォルダに置く）の書き方:
    socks5://user:pass@host:port
    socks5://host:port
    http://user:pass@host:port
    host:port            （→ http://host:port とみなす）
  行頭 # はコメント、空行は無視。ファイルを書き換えると次の貸出から自動で反映。

使い方:
    lease = proxy_pool.acquire()        # 空いているプロキシを1枠借りる（満枠なら空くまで待つ）
    Client(udkey, proxy=lease.url)      # そのClientの通信がこの出口を通る
    lease.release()                     # 使い終わったら返す

  「1セッション」= 動作中のClient1つ（周回ジョブ・垢1件ぶんの作成・ガシャ1回ぶん等）。
  proxy.txt が空のときは acquire() が None を返し、すべて直接接続になる。
"""

import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY_FILE = os.path.join(BASE_DIR, 'proxy.txt')
MAX_SESSIONS_PER_PROXY = 3     # 1プロキシで同時に使えるセッション（Client）数


class ProxyLease:
    """借りたプロキシ1枠。release() で返す（何度呼んでも安全）"""

    __slots__ = ('url', '_pool', '_released')

    def __init__(self, url: str, pool: 'ProxyPool'):
        self.url = url
        self._pool = pool
        self._released = False

    def release(self):
        if not self._released:
            self._released = True
            self._pool._release(self.url)


class ProxyPool:
    """proxy.txt のプロキシを、1プロキシ max_sessions セッションまで貸し出す"""

    def __init__(self, path: str = PROXY_FILE,
                 max_sessions: int = MAX_SESSIONS_PER_PROXY):
        self.path = path
        self.max_sessions = max_sessions
        self._cond = threading.Condition()
        self._proxies: list[str] = []
        self._counts: dict[str, int] = {}      # プロキシURL -> 使用中セッション数
        self._last_used: dict[str, float] = {}  # プロキシURL -> 最後に貸した時刻
        self._mtime: float | None = None

    def _reload(self):
        """proxy.txt を読み込む（ロック取得中に呼ぶこと）。mtime が変わった時だけ更新"""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None
            self._proxies = []
            return
        if mtime == self._mtime:
            return
        proxies = []
        try:
            with open(self.path, encoding='utf-8-sig') as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith('#'):
                        continue
                    if '://' not in s:
                        s = 'http://' + s
                    proxies.append(s)
        except OSError:
            return
        self._mtime = mtime
        self._proxies = proxies
        # ファイルから消えたプロキシも、使い終わっていない分のカウントは残す
        for p in list(self._counts):
            if p not in proxies and self._counts[p] == 0:
                del self._counts[p]
        print(f'[proxy] proxy.txt 読み込み: {len(proxies)}件')

    def acquire(self, timeout: float | None = None) -> ProxyLease | None:
        """1枠借りる。proxy.txt が空なら None（直接接続）。

        すべてのプロキシが満枠のときは、timeout（None=無制限）まで空くのを待つ。
        空き枠の多いプロキシ → 一番使っていないプロキシの順で選び、均等に分散させる。
        """
        with self._cond:
            self._reload()
            if not self._proxies:
                return None
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                free = [p for p in self._proxies
                        if self._counts.get(p, 0) < self.max_sessions]
                if free:
                    p = min(free, key=lambda x: (self._counts.get(x, 0),
                                                 self._last_used.get(x, 0.0)))
                    self._counts[p] = self._counts.get(p, 0) + 1
                    self._last_used[p] = time.monotonic()
                    return ProxyLease(p, self)
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                self._cond.wait(1.0)

    def _release(self, url: str):
        with self._cond:
            n = self._counts.get(url, 0)
            if n > 0:
                self._counts[url] = n - 1
                if self._counts[url] == 0 and url not in self._proxies:
                    del self._counts[url]
            self._cond.notify_all()

    def size(self) -> int:
        """登録されているプロキシの件数（毎回ファイルを確認する）"""
        with self._cond:
            self._reload()
            return len(self._proxies)

    def status(self) -> list[tuple[str, int, int]]:
        """(マスク済みURL, 使用中, 上限) の一覧"""
        with self._cond:
            self._reload()
            return [(mask(p), self._counts.get(p, 0), self.max_sessions)
                    for p in self._proxies]


POOL = ProxyPool()


def acquire(timeout: float | None = None) -> ProxyLease | None:
    return POOL.acquire(timeout)


def size() -> int:
    return POOL.size()


def status() -> list[tuple[str, int, int]]:
    return POOL.status()


def bind(client, lease: ProxyLease):
    """Client に借りた枠を結び付ける（release_client でまとめて返せるように）"""
    client._proxy_lease = lease
    return client


def release_client(client):
    """Client に結び付いた枠を返す（結び付いていなければ何もしない・何度でも呼べる）"""
    lease = getattr(client, '_proxy_lease', None)
    if lease is not None:
        client._proxy_lease = None
        lease.release()


def mask(url: str) -> str:
    """認証情報を隠して scheme://***@host:port の形で見せる"""
    if '://' in url:
        scheme, rest = url.split('://', 1)
        at = rest.find('@')
        if at != -1:
            return f'{scheme}://***@{rest[at + 1:]}'
    return url


def status_text() -> str:
    """起動ログ用の一言サマリ"""
    n = POOL.size()
    if not n:
        return 'なし（proxy.txt が空なので全操作が直接接続）'
    return f'{n}件（1プロキシ同時{MAX_SESSIONS_PER_PROXY}セッションまで）'
