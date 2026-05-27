#!/usr/bin/env python3
"""KTX 취소표 모니터링 · 텔레그램 알림 · 자동 예약"""

import os
import sys
import time
import logging
from pathlib import Path

import requests
import yaml

try:
    from korail2 import Korail, ReserveOption, KorailError
    try:
        from korail2 import SoldOutError
    except ImportError:
        SoldOutError = KorailError
    try:
        from korail2 import NoResultsError
    except ImportError:
        NoResultsError = Exception
except ImportError:
    print("[오류] korail2 라이브러리가 없습니다. 'pip install -r requirements.txt' 를 실행하세요.")
    sys.exit(1)

# 코레일 앱 버전 후보 (최신순) — MACRO ERROR 시 순서대로 재시도
_KORAIL_VERSIONS = [
    '260527001', '260401001', '260301001', '260201001', '260101001',
    '251201001', '251101001', '251001001', '250901001', '250801001',
    '250701001', '250601001', '250501001', '250401001', '250301001',
    '250201001', '250101001', '241201001', '241101001', '241001001',
    '240701001', '240401001', '240101001', '231231001',
]


# ── 로깅 설정 ──────────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ktx_monitor.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger("ktx_monitor")

logger = _setup_logging()


# ── 설정 로드 ──────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    # GitHub Actions 환경: 환경변수로 설정 읽기
    if os.environ.get("KORAIL_ID"):
        return {
            "korail": {
                "id": os.environ["KORAIL_ID"],
                "password": os.environ["KORAIL_PASSWORD"],
            },
            "telegram": {
                "bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
                "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            },
            "search": {
                "departure": os.environ.get("DEPARTURE", "서울"),
                "arrival": os.environ.get("ARRIVAL", "부산"),
                "date": os.environ["DATE"],
                "time_from": os.environ.get("TIME_FROM", "0000"),
                "time_to": os.environ.get("TIME_TO", "2359"),
                "train_types": [t.strip() for t in os.environ.get("TRAIN_TYPES", "KTX").split(",")],
                "seat_types": [s.strip() for s in os.environ.get("SEAT_TYPES", "general").split(",")],
            },
            "monitor": {
                "interval": 10,
                "auto_reserve": os.environ.get("AUTO_RESERVE", "true").lower() == "true",
                "stop_after_reserve": True,
                "max_attempts": 1,
                "notify_on_start": False,
            },
        }
    # 로컬 환경: config.yaml 읽기
    p = Path(path)
    if not p.exists():
        logger.error(f"설정 파일 없음: {path}")
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _validate_config(cfg)
    return cfg


def _validate_config(cfg: dict) -> None:
    placeholders = {"YOUR_KORAIL_ID", "YOUR_PASSWORD", "YOUR_BOT_TOKEN", "YOUR_CHAT_ID"}
    korail_id = cfg.get("korail", {}).get("id", "")
    password  = cfg.get("korail", {}).get("password", "")
    bot_token = cfg.get("telegram", {}).get("bot_token", "")
    chat_id   = cfg.get("telegram", {}).get("chat_id", "")
    if any(v in placeholders for v in [korail_id, password, bot_token, chat_id]):
        logger.error("config.yaml 에 실제 계정 정보와 텔레그램 설정을 입력하세요.")
        sys.exit(1)


# ── 텔레그램 ────────────────────────────────────────────────────────────────────

def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("텔레그램 전송 성공")
        return True
    except requests.RequestException as exc:
        detail = ""
        if hasattr(exc, "response") and exc.response is not None:
            detail = f" → {exc.response.text}"
        logger.error(f"텔레그램 전송 실패: {exc}{detail}")
        return False


# ── 시간 유틸 ──────────────────────────────────────────────────────────────────

def _to_hhmm(raw) -> str:
    """HHMMSS / HH:MM:SS / HHMM / HH:MM → HHMM (4자리 문자열)"""
    s = str(raw).replace(":", "").replace(" ", "")
    return s[:4]


def _fmt_hhmm(raw) -> str:
    h = _to_hhmm(raw)
    return f"{h[:2]}:{h[2:]}"


def _in_range(dep_time_raw, time_from: str, time_to: str) -> bool:
    dep  = _to_hhmm(dep_time_raw)
    low  = _to_hhmm(time_from)
    high = _to_hhmm(time_to)
    return low <= dep <= high


# ── 모니터 클래스 ───────────────────────────────────────────────────────────────

class KTXMonitor:
    def __init__(self, config: dict):
        self.cfg = config
        self.korail: Korail | None = None
        self._ver_idx = 0
        self._login()

    # ── 코레일 로그인 ──────────────────────────────────────────────────────────

    def _login(self) -> None:
        k = self.cfg["korail"]
        logger.info("코레일 로그인 중...")
        self.korail = Korail(k["id"], k["password"], auto_login=True)
        self.korail._version = _KORAIL_VERSIONS[self._ver_idx]
        logger.info(f"코레일 로그인 성공 (버전: {_KORAIL_VERSIONS[self._ver_idx]})")

    def _next_version(self) -> None:
        """MACRO ERROR 발생 시 다음 버전 후보로 교체"""
        self._ver_idx += 1
        if self._ver_idx >= len(_KORAIL_VERSIONS):
            raise RuntimeError("모든 버전 후보 소진 — korail2 라이브러리 업데이트 필요")
        v = _KORAIL_VERSIONS[self._ver_idx]
        self.korail._version = v
        logger.info(f"버전 변경 → {v}")

    def _try_relogin(self) -> None:
        logger.warning("세션 만료 감지 — 재로그인 시도")
        for attempt in range(1, 4):
            try:
                self._login()
                return
            except Exception as exc:
                logger.error(f"재로그인 실패 ({attempt}/3): {exc}")
                time.sleep(5 * attempt)
        raise RuntimeError("재로그인 3회 실패, 모니터링 중단")

    # ── 열차 조회 ──────────────────────────────────────────────────────────────

    def _find_available(self) -> list[tuple]:
        """잔여석 있는 (train, has_general, has_special) 목록 반환"""
        s = self.cfg["search"]
        dep        = s["departure"]
        arr        = s["arrival"]
        date       = s["date"]
        time_from  = s.get("time_from", "0000")
        time_to    = s.get("time_to", "2359")
        seat_types = s.get("seat_types", ["general"])
        train_types = [t.upper().replace(" ", "") for t in s.get("train_types", [])]

        logger.info(f"[조회] {dep} → {arr}  {date}  {_fmt_hhmm(time_from)}~{_fmt_hhmm(time_to)}")

        trains = self.korail.search_train(dep, arr, date, _to_hhmm(time_from))
        result = []

        for train in trains:
            if not _in_range(train.dep_time, time_from, time_to):
                continue

            if train_types:
                t_upper = str(train.train_type).upper().replace(" ", "")
                if not any(tt in t_upper for tt in train_types):
                    continue

            has_general = "general" in seat_types and train.has_general_seat()
            has_special = "special"  in seat_types and train.has_special_seat()

            if has_general or has_special:
                result.append((train, has_general, has_special))

        return result

    # ── 예약 ───────────────────────────────────────────────────────────────────

    def _reserve(self, train, has_general: bool, has_special: bool):
        option = ReserveOption.GENERAL_FIRST if has_general else ReserveOption.SPECIAL_FIRST
        return self.korail.reserve(train, option=option)

    # ── 메시지 생성 ────────────────────────────────────────────────────────────

    @staticmethod
    def _ticket_msg(train, has_general: bool, has_special: bool, date: str) -> str:
        seats = []
        if has_general: seats.append("일반실")
        if has_special: seats.append("특실")
        dep_t = _fmt_hhmm(train.dep_time)
        arr_t = _fmt_hhmm(train.arr_time)
        ymd = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        return (
            f"🚄 <b>KTX 취소표 발견!</b>\n\n"
            f"열차: {train.train_type} {train.train_no}\n"
            f"구간: {train.dep_name} → {train.arr_name}\n"
            f"출발: {ymd} {dep_t}\n"
            f"도착: {arr_t}\n"
            f"좌석: {', '.join(seats)}"
        )

    # ── 메인 루프 ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        tg  = self.cfg["telegram"]
        mon = self.cfg["monitor"]
        s   = self.cfg["search"]

        interval          = max(int(mon.get("interval", 30)), 10)
        max_attempts      = int(mon.get("max_attempts", 0))
        auto_reserve      = bool(mon.get("auto_reserve", False))
        stop_after_rsv    = bool(mon.get("stop_after_reserve", True))
        notify_on_start   = bool(mon.get("notify_on_start", True))

        def tg_send(text: str):
            send_telegram(tg["bot_token"], str(tg["chat_id"]), text)

        if notify_on_start:
            ymd = f"{s['date'][:4]}-{s['date'][4:6]}-{s['date'][6:]}"
            tg_send(
                f"🔍 <b>KTX 취소표 모니터링 시작</b>\n"
                f"구간: {s['departure']} → {s['arrival']}\n"
                f"날짜: {ymd}\n"
                f"시간: {_fmt_hhmm(s.get('time_from','0000'))} ~ {_fmt_hhmm(s.get('time_to','2359'))}\n"
                f"자동예약: {'✅' if auto_reserve else '❌ (알림만)'}\n"
                f"조회간격: {interval}초"
            )

        attempt = 0
        while True:
            attempt += 1
            if max_attempts > 0 and attempt > max_attempts:
                logger.info(f"최대 시도 횟수({max_attempts}) 도달 — 종료")
                tg_send(f"⏹ 최대 시도 횟수 {max_attempts}회 도달, 모니터링 종료")
                break

            try:
                available = self._find_available()

                if not available:
                    logger.info(f"잔여석 없음 (시도 #{attempt})")
                else:
                    for train, has_general, has_special in available:
                        msg = self._ticket_msg(train, has_general, has_special, s["date"])

                        if not auto_reserve:
                            tg_send(msg)
                            logger.info(f"취소표 알림 전송: {train.train_type} {train.train_no}")
                            continue

                        # 자동 예약 시도
                        try:
                            reservation = self._reserve(train, has_general, has_special)
                            rsv_id = getattr(reservation, "rsv_id", "N/A")
                            msg += f"\n\n✅ <b>자동 예약 완료!</b>\n예약번호: {rsv_id}"
                            tg_send(msg)
                            logger.info(f"예약 완료: {rsv_id}")
                            if stop_after_rsv:
                                logger.info("모니터링 종료")
                                return
                        except SoldOutError:
                            msg += "\n\n⚠️ 예약 도중 매진 — 계속 모니터링"
                            tg_send(msg)
                            logger.warning("예약 도중 매진")
                        except KorailError as exc:
                            msg += f"\n\n❌ 예약 실패: {exc}"
                            tg_send(msg)
                            logger.error(f"예약 실패: {exc}")

            except NoResultsError:
                logger.info(f"조회 결과 없음 (시도 #{attempt})")

            except KorailError as exc:
                err = str(exc)
                logger.error(f"코레일 오류: {err}")
                if "MACRO" in err:
                    try:
                        self._next_version()
                    except RuntimeError as fatal:
                        tg_send(f"🚨 {fatal}")
                        sys.exit(1)
                    continue  # 대기 없이 즉시 재시도
                elif any(k in err.lower() for k in ("로그인", "login", "session", "expire")):
                    try:
                        self._try_relogin()
                    except RuntimeError as fatal:
                        tg_send(f"🚨 {fatal}")
                        sys.exit(1)

            except Exception as exc:
                logger.error(f"예상치 못한 오류: {exc}", exc_info=True)

            time.sleep(interval)


# ── 진입점 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()
    KTXMonitor(cfg).run()


if __name__ == "__main__":
    main()
