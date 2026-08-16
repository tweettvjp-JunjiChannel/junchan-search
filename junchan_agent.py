"""
ntfy経由の「確認が取れるまで再通知し続ける」緊急アラームエージェント。
標準ライブラリのみ（urllib, json, time, winsound, msvcrt 等）で実装する。

仕組み：
- trigger_emergency_alarm() は alert_topic へ「了解 (ACK)」アクションボタン
  付きの通知を送信する。このボタンはntfyアプリの action=http 機能により、
  タップされるとアプリを開かずバックグラウンドで ack_topic へ自動的に
  POSTリクエストを送る。
- スクリプト側は interval_seconds ごとに ack_topic をポーリングする一方、
  待機中は msvcrt でPCのキー入力も並行して監視し、[Enter] が押されれば
  スマホを確認できない状況でもその場でACKとみなしてループを終了する。
  どちらの経路でACKが確定しても、完了通知を _send_plain_notification で
  改めてスマホへ送る。
- 通知の送信タイミング（初回・再通知）ではPCのビープ音も鳴らし、
  スマホが手元になくてもPCの近くにいれば気づけるようにする。
- `--success` 引数を付けて実行すると「正常完了モード」になり、短い
  ビープ＋「✅ 作業完了」通知を1回だけ送って即座に終了する（ACK待機
  ループには入らない）。緊急停止用の trigger_emergency_alarm() とは
  完全に独立した経路（_send_plain_notification を直接使う）で送るため、
  actionsペイロードの組み立てミス等が完了通知にまで波及しない。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import winsound
    _WINSOUND_AVAILABLE = True
except ImportError:
    _WINSOUND_AVAILABLE = False

try:
    import msvcrt
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False


class JunchanAgent:
    def __init__(
        self,
        alert_topic: str = "junchan2026",
        ack_topic: str = "junchan2026-ack",
        interval_seconds: int = 60,
        base_url: str = "https://ntfy.sh",
    ):
        self.alert_topic = alert_topic
        self.ack_topic = ack_topic
        self.interval_seconds = interval_seconds
        self.base_url = base_url.rstrip("/")

    def _beep(self, freq: int = 1000, duration_ms: int = 500, repeats: int = 3, gap: float = 0.1) -> None:
        if not _WINSOUND_AVAILABLE:
            print("[警告] winsoundが利用できない環境のため、ビープ音をスキップします。")
            return
        try:
            for _ in range(repeats):
                winsound.Beep(freq, duration_ms)
                time.sleep(gap)
        except RuntimeError as e:
            print(f"[警告] ビープ音の再生に失敗しました: {e}")

    def _send_alert(self, message: str, title: str) -> int:
        payload = {
            "topic": self.alert_topic,
            "title": title,
            "message": message,
            "priority": 5,
            "tags": ["rotating_light"],
            "actions": [
                {
                    "action": "http",
                    "label": "了解 (ACK)",
                    "url": f"{self.base_url}/{self.ack_topic}",
                    "method": "POST",
                    "body": "ack",
                    "clear": True,
                }
            ],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status

    def _send_plain_notification(self, message: str, title: str) -> int:
        """アクションボタン無しの通常メッセージを送る単純なリクエスト。
        ACK検知直後の完了通知は _send_alert とロジックを共有させず、
        actionsペイロードの組み立てミス等が完了通知にまで波及しないようにする。
        """
        payload = {
            "topic": self.alert_topic,
            "title": title,
            "message": message,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status

    def _has_ack_since(self, since_epoch: float) -> bool:
        query = urllib.parse.urlencode({"poll": "1", "since": str(int(since_epoch))})
        url = f"{self.base_url}/{self.ack_topic}/json?{query}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
        except Exception as e:
            print(f"[警告] ACK確認中にエラー: {e}")
            return False

        for line in body.strip().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("event") == "message":
                return True
        return False

    def _wait_with_local_ack(self, seconds: int) -> bool:
        """interval_seconds待機しつつ、コンソールに残り秒数を表示し、
        並行してPCの[Enter]キー入力を監視する。
        戻り値: [Enter]が押された場合True、待機完了までなければFalse。
        """
        if _MSVCRT_AVAILABLE:
            # 待機開始前に溜まっている古いキー入力を読み捨てる
            while msvcrt.kbhit():
                msvcrt.getch()

        for remaining in range(seconds, 0, -1):
            label = (
                f"[順ちゃんAI] 🚨 緊急停止中: スマホの「了解」を長押し、"
                f"またはこの画面で [Enter] キーを押すと再開・解除します "
                f"(次回再通知まで残り {remaining:3d} 秒)..."
            )
            print(f"\r{label}", end="", flush=True)

            segment_start = time.time()
            while time.time() - segment_start < 1.0:
                if _MSVCRT_AVAILABLE and msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch in (b"\r", b"\n"):
                        print()  # カウントダウン行から改行する
                        return True
                time.sleep(0.05)

        print()  # カウントダウン終了後に改行する
        return False

    def _finish_ack(self, source: str) -> None:
        print(f"[ACK受信] {source} により「了解」が確認されました。完了通知を送信します。")
        try:
            status = self._send_plain_notification(
                "【了解確認】手動対応の完了を待機中", "了解確認"
            )
            print(f"[完了通知] 送信成功 status={status}")
        except Exception as e:
            print(f"[エラー] 完了通知の送信に失敗しました: {e}")
        print("ループを正常終了します。")

    def trigger_emergency_alarm(
        self,
        message: str = "確認してください。この通知は「了解」が押されるまで繰り返し送信されます。",
        title: str = "緊急通知",
        max_attempts: int | None = None,
    ) -> bool:
        start_epoch = time.time()
        attempt = 0

        print(
            f"[開始] alert_topic={self.alert_topic} / ack_topic={self.ack_topic} "
            f"/ interval={self.interval_seconds}秒"
        )

        while True:
            attempt += 1
            status = self._send_alert(message, title)
            self._beep()
            print(f"[{attempt}回目] 通知送信 status={status}")

            if self._wait_with_local_ack(self.interval_seconds):
                self._finish_ack("PC (Enterキー)")
                return True

            if self._has_ack_since(start_epoch):
                self._finish_ack("スマホ (ACK)")
                return True

            if max_attempts is not None and attempt >= max_attempts:
                print(f"[終了] 最大試行回数（{max_attempts}回）に達したため終了します。ACKは受信されませんでした。")
                return False

    def trigger_success_notification(
        self,
        message: str = "任されたタスクが正常に完了しました。",
        title: str = "✅ 作業完了",
    ) -> bool:
        """タスク正常完了時の通知。緊急停止モードと異なりACK待機ループには
        入らず、短いビープと通知を1回ずつ出したら即座に終了する。
        """
        print(f"[正常完了] {self.alert_topic} へ完了通知を送信します。")
        self._beep(freq=1500, duration_ms=150, repeats=3, gap=0.05)

        try:
            status = self._send_plain_notification(message, title)
            print(f"[完了通知] 送信成功 status={status}")
            return True
        except Exception as e:
            print(f"[エラー] 完了通知の送信に失敗しました: {e}")
            return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="順ちゃんAI緊急停止/完了通知エージェント")
    parser.add_argument(
        "--success",
        action="store_true",
        help="正常完了モード：完了通知を1回送りビープを鳴らして即終了する（ACK待機ループには入らない）",
    )
    args = parser.parse_args()

    agent = JunchanAgent()

    if args.success:
        ok = agent.trigger_success_notification()
        sys.exit(0 if ok else 1)

    result = agent.trigger_emergency_alarm()
    sys.exit(0 if result else 1)
