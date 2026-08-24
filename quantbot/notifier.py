import os
import time
import logging
import threading
from typing import Optional, Dict, Callable
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TelegramNotifier")


class TelegramNotifier:
    """
    텔레그램 메신저 알림 발송 및 대화형 명령어(/자산, /status) 수신 백그라운드 스레드 모듈.
    """

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None,
                 enabled: bool = True):
        """
        :param enabled: False면 토큰이 있어도 알림/명령 수신을 사용하지 않습니다.
            인스턴스를 여러 개 띄울 때 같은 봇 토큰으로 폴링하면 명령이 뒤섞이므로,
            한 인스턴스에서만 켜기 위한 스위치입니다.
        """
        load_dotenv()
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.is_enabled = True

        if not enabled:
            logger.info("텔레그램이 설정으로 비활성화되었습니다. (telegram_enabled=false)")
            self.is_enabled = False
            return

        self.is_polling = False
        self.polling_thread: Optional[threading.Thread] = None
        self.command_handlers: Dict[str, Callable[[], str]] = {}

        # 토큰 유효성 기본 검증
        is_invalid_token = not self.bot_token or "your_telegram_bot_token" in self.bot_token
        is_invalid_chat = not self.chat_id or "your_telegram_chat_id" in self.chat_id

        if is_invalid_token or is_invalid_chat:
            logger.warning("텔레그램 BOT_TOKEN 또는 CHAT_ID가 설정되지 않았습니다. [알림 비활성화 모드]")
            self.is_enabled = False
        else:
            logger.info("텔레그램 알림 모듈 정상 등록 완료")

    def send_message(self, message: str) -> bool:
        """
        텔레그램으로 메시지 발송

        :param message: 발송할 텍스트 메시지
        :return: 성공 여부 (bool)
        """
        if not self.is_enabled:
            logger.debug(f"[알림 스킵 - 비활성화] 메시지: {message}")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }

        try:
            response = requests.post(url, data=payload, timeout=5.0)
            if response.status_code == 200:
                logger.info("텔레그램 메시지 발송 성공")
                return True
            else:
                logger.warning(f"텔레그램 메시지 발송 실패 (Status: {response.status_code}): {response.text}")
                return False
        except Exception as e:
            logger.error(f"텔레그램 API 통신 중 예외 발생: {e}")
            return False

    def start_polling(self, command_handlers: Dict[str, Callable[[], str]]):
        """
        텔레그램 명령어 수신 Polling 백그라운드 스레드 가동

        :param command_handlers: 명령어('/자산', '/status' 등)와 콜백 함수 매핑 딕셔너리
        """
        if not self.is_enabled:
            logger.warning("텔레그램이 비활성화 상태이므로 Polling 스레드를 가동하지 않습니다.")
            return

        self.command_handlers = command_handlers
        self.is_polling = True
        self.polling_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.polling_thread.start()
        logger.info("텔레그램 대화형 명령어 수신 Polling 스레드가 백그라운드에서 가동되었습니다.")

    def stop_polling(self):
        """Polling 스레드 중지"""
        self.is_polling = False

    def _polling_loop(self):
        """Telegram getUpdates 엔드포인트를 사용한 롱 폴링 루프"""
        offset = 0
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"

        while self.is_polling:
            try:
                params = {"offset": offset, "timeout": 5}
                response = requests.get(url, params=params, timeout=7.0)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") and data.get("result"):
                        for update in data["result"]:
                            offset = update["update_id"] + 1
                            message = update.get("message", {})
                            text = message.get("text", "").strip()
                            incoming_chat_id = str(message.get("chat", {}).get("id", ""))

                            # 지정된 authorized chat_id의 메시지인 경우 명령어 처리
                            if text and (incoming_chat_id == str(self.chat_id) or not self.chat_id):
                                logger.info(f"텔레그램 수신 명령어: '{text}' (Chat ID: {incoming_chat_id})")
                                self._dispatch_command(text)

            except Exception as e:
                logger.debug(f"텔레그램 Polling 네트워크 일시적 예외: {e}")
                time.sleep(2.0)

            time.sleep(1.0)

    def _dispatch_command(self, raw_text: str):
        """수신된 텍스트 명령어를 파싱하고 매핑된 콜백 실행 후 응답 전송"""
        cmd = raw_text.split("@")[0].strip().lower()  # '/자산@botname' 처리

        if cmd in self.command_handlers:
            handler_func = self.command_handlers[cmd]
            try:
                reply_text = handler_func()
                self.send_message(reply_text)
            except Exception as e:
                err_reply = f"🚨 <b>[명령어 처리 오류]</b> {cmd} 실행 중 에러 발생: {e}"
                self.send_message(err_reply)
        elif cmd.startswith("/"):
            help_text = (
                "🤖 <b>[QuantBot 명령어 안내]</b>\n"
                "• <b>/자산</b> 또는 <b>/balance</b> : 원화 및 보유 코인 실시간 평가 현황\n"
                "• <b>/상태</b> 또는 <b>/status</b> : 봇 가동 상태 및 종목별 목표가/동적 K값 현황\n"
                "• <b>/재산정</b> : 최신 입출금·잔고로 목표가와 ATR 목표수량 다시 계산"
            )
            self.send_message(help_text)


if __name__ == "__main__":
    print("=" * 70)
    print("[TelegramNotifier 대화형 명령어 수신 테스트]")
    print("=" * 70)

    def mock_balance():
        return "💰 [모의 응답] 총 자산: 1,000,000 원"

    def mock_status():
        return "🤖 [모의 응답] QuantBot 24시간 가동 중"

    notifier = TelegramNotifier()
    notifier.start_polling({
        "/자산": mock_balance,
        "/balance": mock_balance,
        "/상태": mock_status,
        "/status": mock_status
    })

    print("  - Polling 스레드 가동 완료. 5초간 대기...")
    time.sleep(5)
    notifier.stop_polling()
    print("=" * 70)
