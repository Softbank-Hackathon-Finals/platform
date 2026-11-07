"""로컬 증명 전용 핸들러: 제한 시간을 넘기는 함수. 시간 초과도 재시도 한도 뒤 실패 보관 큐로 가는지 본다."""

import time


def handler(event, context):
    time.sleep(context.get_remaining_time_in_millis() / 1000 + 5)
