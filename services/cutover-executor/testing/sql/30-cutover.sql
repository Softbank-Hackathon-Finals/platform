-- barrier · probe 테이블(spec D7)과 복제 권한 · publication. 앱 스키마(demo-chat db/schema.sql) 다음에 적용한다.

CREATE SCHEMA cutover;
-- 5단계: 제어 역할이 쓰고, 목적지에 도착했는지 본다
CREATE TABLE cutover.barrier (
    barrier_id uuid        PRIMARY KEY,
    epoch      bigint      NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- 8단계: 앱 역할로 각 PgBouncer 경로를 거쳐 쓰고, 그 응답(ACK)으로 쓰기 정지 시간을 끝낸다
CREATE TABLE cutover.probe (
    probe_id   uuid        PRIMARY KEY,
    path       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA cutover TO cutover_control, chat_app, replicator, subscriber;
GRANT SELECT, INSERT ON cutover.barrier TO cutover_control;
GRANT SELECT ON cutover.probe TO cutover_control;
GRANT INSERT ON cutover.probe TO chat_app;
-- 도착 확인 · 비교와 시퀀스 올리기
GRANT SELECT ON public.attachments, public.messages TO cutover_control;
GRANT SELECT, UPDATE ON SEQUENCE public.messages_id_seq TO cutover_control;

GRANT SELECT ON public.attachments, public.messages, cutover.barrier, cutover.probe TO replicator;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.attachments, public.messages, cutover.barrier, cutover.probe TO subscriber;

CREATE PUBLICATION bravo_pub
    FOR TABLE public.attachments, public.messages, cutover.barrier, cutover.probe;
