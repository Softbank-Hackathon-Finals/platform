-- 모의 환경의 역할. 실제 이름 · 권한은 T6 · T8 담당과 확정한다(spec 7절).
-- 이 파일의 cutover_control 권한이 Executor가 필요한 최소 권한 목록의 초안이다.
-- psql 변수: chat_app_pw, replicator_pw, control_pw, chat_app_login(LOGIN | NOLOGIN)

-- 앱 역할. 목적지에서는 컷오버 7단계 전까지 NOLOGIN이다(쓰기 DB는 최대 하나).
CREATE ROLE chat_app :chat_app_login PASSWORD :'chat_app_pw';

-- 복제: 발행 쪽에 접속하는 역할과, 받는 쪽에서 구독을 소유하고 적용하는 역할
CREATE ROLE replicator LOGIN REPLICATION PASSWORD :'replicator_pw';
-- 구독 적용 작업이 소유자로 로그인하므로 LOGIN이 필요하다. 비밀번호가 없어 바깥에서는 접속할 수 없다.
CREATE ROLE subscriber LOGIN;
GRANT pg_create_subscription TO subscriber;
GRANT CREATE ON DATABASE chat TO subscriber;

-- Executor 제어 역할
CREATE ROLE cutover_control LOGIN CREATEROLE PASSWORD :'control_pw';
-- 펜싱: chat_app의 LOGIN 속성 변경 (PG16: CREATEROLE + ADMIN OPTION). chat_app 권한은 물려받지 않는다.
GRANT chat_app TO cutover_control WITH ADMIN OPTION, INHERIT FALSE, SET FALSE;
-- 구독 ENABLE · DISABLE: SET ROLE subscriber로만 한다. 평소에는 subscriber의 테이블 쓰기 권한을 물려받지 않는다.
GRANT subscriber TO cutover_control WITH INHERIT FALSE, SET TRUE;
-- 남은 앱 세션 종료, 다른 역할의 pg_stat_activity 조회(긴 트랜잭션 확인)
GRANT pg_signal_backend, pg_read_all_stats TO cutover_control;
