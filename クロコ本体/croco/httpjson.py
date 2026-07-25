"""JSON over HTTP の最小クライアント（標準ライブラリのみ）。

Notion / Gemini とも素のREST APIなので、SDKを入れずにこれ1つで足りる。
リトライ方針は「一時的な失敗だけ再試行し、恒久的な失敗は即座に諦める」。
恒久的な失敗（認証エラー・不正なリクエスト等）を再試行しても状況は変わらず、
無人実行では黙って時間を溶かすだけなので、エラー種別で分岐させる。
"""

from __future__ import annotations

import json
import random
import ssl
import time
import urllib.error
import urllib.request

# 一時的とみなすHTTPステータス。これ以外は再試行しない。
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT = 60.0
DEFAULT_ATTEMPTS = 4

# --- TLS ---------------------------------------------------------------
#
# このPCではNortonのSSL/TLSスキャンが全通信を傍受しており、Nortonが動的生成する
# CA証明書の BasicConstraints 拡張が critical 指定されていない。
# Python 3.13以降 `VERIFY_X509_STRICT`（RFCへの厳格適合チェック）が既定で有効に
# なったため、これがそのまま検証エラーになる。
#
# 対処として STRICT のみを外す。**証明書チェーンの信頼検証・ホスト名照合・
# 有効期限の確認はそのまま効いている**（形式の細かな不備を許容するだけ）。
# 検証自体を無効化するのとは全く別物で、信頼できない証明書は今も弾かれる。
#
# 恒久的な解決を望むなら、Norton側で「暗号化された接続のスキャン」を無効にするか、
# 対象ホストを除外する。そうすれば STRICT のままで通るようになる。

_STRICT_HINTS = ("not marked critical", "invalid ca certificate", "x509")

_relaxed_context: ssl.SSLContext | None = None
_use_relaxed = False


def _relaxed() -> ssl.SSLContext:
    global _relaxed_context
    if _relaxed_context is None:
        context = ssl.create_default_context()
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        _relaxed_context = context
    return _relaxed_context


def _looks_like_strict_failure(exc: ssl.SSLCertVerificationError) -> bool:
    message = f"{exc.verify_message or ''} {exc}".lower()
    return any(hint in message for hint in _STRICT_HINTS)


def _warn_relaxed(exc: ssl.SSLCertVerificationError) -> None:
    from . import log as _log

    _log.warn(
        "証明書の厳格適合チェック(VERIFY_X509_STRICT)で失敗したため、"
        f"このチェックのみ外して再試行します（{exc.verify_message}）。"
        " 信頼検証・ホスト名照合・有効期限の確認は引き続き有効です。"
        " このPCではNortonのSSLスキャンが原因。Norton側で暗号化接続の"
        "スキャンを無効にすれば、厳格チェックのままで通るようになります。"
    )


class HttpError(RuntimeError):
    """HTTPレベルの失敗。`status` と `body` で原因を判別できるようにする。"""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}\n{body[:2000]}")
        self.status = status
        self.body = body
        self.url = url

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS


def _sleep_for_attempt(attempt: int, retry_after: str | None) -> None:
    """指数バックオフ。サーバが Retry-After を返していればそれを優先する。"""
    if retry_after:
        try:
            time.sleep(min(float(retry_after), 60.0))
            return
        except ValueError:
            pass
    # ジッタを入れて、複数リクエストが同じ間隔で再突入するのを避ける。
    delay = min(2.0 ** attempt, 30.0) * (0.5 + random.random() / 2)
    time.sleep(delay)


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
) -> dict:
    """JSONを投げてJSONを受け取る。失敗時は HttpError を投げる。

    レスポンスが空（204等）の場合は空の辞書を返す。
    """
    body: bytes | None = None
    all_headers = {"Accept": "application/json"}
    if headers:
        all_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json")

    last_error: Exception | None = None
    global _use_relaxed

    attempt = 0
    while attempt < attempts:
        request = urllib.request.Request(
            url, data=body, headers=all_headers, method=method
        )
        context = _relaxed() if _use_relaxed else None
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=context
            ) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = HttpError(exc.code, detail, url)
            last_error = error
            if not error.retryable or attempt == attempts - 1:
                raise error from exc
            _sleep_for_attempt(attempt, exc.headers.get("Retry-After"))
            attempt += 1
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc

            # urllib は SSL の検証失敗を URLError にくるんで投げてくるため、
            # 中身を見て判別する必要がある。
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError):
                # 厳格チェックだけが原因なら、緩めて再試行する。
                # それ以外の検証失敗（信頼できない証明書等）はそのまま失敗させる。
                if _use_relaxed or not _looks_like_strict_failure(reason):
                    raise
                _use_relaxed = True
                _warn_relaxed(reason)
                # 設定を直しての再試行なので、試行回数は消費しない。
                continue

            # ネットワーク断・DNS失敗など。PC起動直後はまだ回線が
            # 上がりきっていないことがあるため、ここは必ず再試行する。
            if attempt == attempts - 1:
                raise
            _sleep_for_attempt(attempt, None)
            attempt += 1

    # ループは必ず return か raise で抜けるが、型検査のため。
    raise last_error if last_error else RuntimeError("unreachable")
