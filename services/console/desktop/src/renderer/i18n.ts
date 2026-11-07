// UI language (Settings › General › Language, as in Codex). Korean is the source text; Japanese and
// English come from this table. Agent replies follow the language of the question.
import { useAtomValue } from 'jotai';
import { atomWithStorage } from 'jotai/utils';

export type Lang = 'ko' | 'ja' | 'en';
export const LANGS: { id: Lang; label: string }[] = [{ id: 'ko', label: '한국어' }, { id: 'ja', label: '日本語' }, { id: 'en', label: 'English' }];
export const langAtom = atomWithStorage<Lang>('sova.lang', 'ko');

const D: Record<string, [ja: string, en: string]> = {
  '대시보드': ['ダッシュボード', 'Dashboard'], '환경·앱 등록': ['環境・アプリ登録', 'Register'], '이전 계획': ['移行計画', 'Migration plans'], '진행 타임라인': ['進行タイムライン', 'Timeline'],
  '관측 · 보안': ['観測・セキュリティ', 'Observe & security'], '처음 안내': ['はじめに', 'Getting started'], '연결 설정': ['接続設定', 'Connection'],
  '새 채팅': ['新しいチャット', 'New chat'], '채팅': ['チャット', 'Chats'], '보관함': ['アーカイブ', 'Archive'], '채팅 검색': ['チャットを検索', 'Search chats'],
  '보관된 채팅 검색': ['アーカイブを検索', 'Search archive'], '고정됨': ['固定', 'Pinned'], '최근': ['最近', 'Recent'],
  '최근 업데이트': ['最近の更新', 'Recently updated'], '생성일': ['作成日', 'Created'], '사이드바로 돌아가기': ['サイドバーに戻る', 'Back to sidebar'],
  '이름 변경': ['名前を変更', 'Rename'], '고정': ['固定', 'Pin'], '고정 해제': ['固定を解除', 'Unpin'], '읽지 않음으로 표시': ['未読にする', 'Mark as unread'],
  '채팅 분기': ['チャットを分岐', 'Fork chat'], 'Markdown으로 복사': ['Markdownでコピー', 'Copy as Markdown'], '보관': ['アーカイブ', 'Archive'],
  '중지하고 보관': ['停止してアーカイブ', 'Stop and archive'], '삭제': ['削除', 'Delete'], '취소': ['キャンセル', 'Cancel'], '복원': ['復元', 'Restore'],
  '채팅을 삭제할까요?': ['チャットを削除しますか?', 'Delete this chat?'],
  'SOVA MCP 연결됨': ['SOVA MCP 接続済み', 'SOVA MCP connected'], 'SOVA MCP 미연결': ['SOVA MCP 未接続', 'SOVA MCP not connected'],
  '읽기 전용': ['読み取り専用', 'Read-only'], '등록 허용': ['登録を許可', 'Registration allowed'], '계획': ['計画', 'Plan'], '계획 없음': ['計画なし', 'No plans'],
  '로컬 시험 데이터': ['ローカル試験データ', 'Local test data'], '조회 실패': ['取得失敗', 'Read failed'], '조회 중': ['取得中', 'Reading'],
  '이동': ['移動', 'Go to'], '안내자에게': ['ガイドに聞く', 'Ask the guide'],
  'SOVA 안내자': ['SOVA ガイド', 'SOVA guide'], '무엇을 옮길까요?': ['何を移行しますか?', 'What shall we move?'],
  '작업 중': ['作業中', 'Working'], '생각 중': ['考え中', 'Thinking'], '동안 작업함': ['作業しました', 'worked'], '도구': ['ツール', 'tools'],
  '모델 선택': ['モデルを選択', 'Choose model'], '추론 수준': ['推論レベル', 'Reasoning'], 'SOVA 작업을 어떻게 승인할까요?': ['SOVA の操作をどう承認しますか?', 'How should SOVA actions be approved?'],
  '컨텍스트': ['コンテキスト', 'Context'], 'MCP 연결됨': ['MCP 接続済み', 'MCP connected'], 'MCP 미연결': ['MCP 未接続', 'MCP offline'],
  '채팅 검색…': ['チャット内を検索…', 'Find in chat…'], '0개의 결과': ['0 件', 'No results'],
  'SOVA에게 뭐든 물어봐. /로 명령, @로 계획·환경 언급': ['SOVA に何でも聞いてください。/ でコマンド、@ で計画・環境を指定', 'Ask SOVA anything. / for commands, @ to mention plans and environments'],
  '작업 중에 보내면 대기열에 넣습니다 · ⌘↩ 지금 조정': ['作業中の送信はキューに入ります · ⌘↩ で今すぐ調整', 'Sending while working queues it · ⌘↩ to steer now'],
  '조정': ['調整', 'Steer'], '지금': ['今', 'Now'],
  '설정': ['設定', 'Settings'], '앱으로 돌아가기': ['アプリに戻る', 'Back to app'], '일반': ['一般', 'General'], '언어': ['言語', 'Language'],
  '연결': ['接続', 'Connection'], '안내자': ['ガイド', 'Guide'], '클라우드 계정': ['クラウドアカウント', 'Cloud accounts'], '키보드 단축키': ['キーボードショートカット', 'Keyboard shortcuts'],
  '개인': ['個人', 'Personal'], '통합': ['連携', 'Integrations'],
  '화면 언어': ['表示言語', 'Display language'], '메뉴·버튼·안내 문구의 언어입니다. 안내자는 질문한 언어로 답합니다.': ['メニュー・ボタン・案内文の言語です。ガイドは質問の言語で答えます。', 'Language for menus, buttons and help text. The guide answers in the language you ask in.'],
  '동작 줄이기': ['動きを減らす', 'Reduce motion'], '애니메이션을 줄입니다.': ['アニメーションを減らします。', 'Reduces animation.'],
  '현재 단계': ['現在の段階', 'Current stage'], '원본 대조 게이트': ['原本照合ゲート', 'Source comparison gate'], '실행 기록': ['実行記録', 'Execution records'],
  '사람 결정 대기': ['人の判断待ち', 'Waiting for a person'], '진행 이벤트': ['進行イベント', 'Events'], '다음 행동 (서버 계산)': ['次の行動(サーバー算出)', 'Next actions (server)'],
  '환경 관측': ['環境の観測', 'Environment metrics'], '등록된 환경': ['登録環境', 'Registered environments'], '등록된 앱': ['登録アプリ', 'Registered apps'],
  '수집': ['収集', 'Collect'], '분석': ['分析', 'Analyze'], '제안': ['提案', 'Propose'], '리허설': ['リハーサル', 'Rehearse'], '원본 대조': ['原本照合', 'Compare'],
  '승인 대기': ['承認待ち', 'Approval'], '전환': ['切替', 'Shift'], '컷오버': ['カットオーバー', 'Cutover'], '관찰': ['監視', 'Watch'], '완료': ['完了', 'Done'],
  '복귀 중': ['切り戻し中', 'Failing back'], '안전 정지': ['安全停止', 'Safe hold'], '사람 판단': ['人の判断', 'Needs a person'], '복귀 완료': ['切り戻し完了', 'Rolled back'],
  '사람': ['人', 'Human'], '출발지': ['移行元', 'Source'], '목적지': ['移行先', 'Target'], '미관측': ['未観測', 'Unobserved'],
  '콘솔 열기': ['コンソールを開く', 'Open console'], '단계 변경': ['段階変更', 'Stage'], '승인 요청': ['承認依頼', 'Approval requested'], '승인 결정': ['承認決定', 'Approval decided'],
  '실행': ['実行', 'Execution'], '복귀': ['切り戻し', 'Failback'], '전체': ['すべて', 'All'], '전체 이벤트': ['全イベント', 'All events'],
  '연결됨': ['接続済み', 'Connected'],
  '설정 필요': ['設定が必要', 'Needs setup'],
  'EKS 클러스터': ['EKS クラスター', 'EKS clusters'],
  '콘솔 홈': ['コンソールホーム', 'Console home'],
  'GKE 클러스터': ['GKE クラスター', 'GKE clusters'],
  'Tailscale 기기': ['Tailscale デバイス', 'Tailscale machines'],
  '보관된 채팅': ['アーカイブ済みチャット', 'Archived chats'],
  'IAM Identity Center 포털을 거쳐 이 역할로 콘솔을 엽니다. 브라우저의 SSO 세션을 씁니다.': ['IAM Identity Center ポータル経由でこのロールのコンソールを開きます。ブラウザの SSO セッションを使います。', 'Opens the console with this role through the IAM Identity Center portal, using your browser SSO session.'],
  '프로젝트 ID를 넣으면 Google 계정으로 콘솔을 엽니다': ['プロジェクト ID を入れると Google アカウントでコンソールを開きます', 'Enter a project ID to open the console with your Google account'],
  '온프렘 목적지는 Tailscale 고정 이름으로 들어갑니다': ['オンプレ移行先には Tailscale の固定名で入ります', 'The on-prem target is reached by its fixed Tailscale name'],
  '프로젝트 ID': ['プロジェクト ID', 'Project ID'],
  'tailnet 이름 (선택)': ['tailnet 名(任意)', 'Tailnet name (optional)'],
  '온프렘 · Tailscale': ['オンプレ・Tailscale', 'On-prem · Tailscale'],
};

export function translate(lang: Lang, ko: string) {
  if (lang === 'ko') return ko;
  const row = D[ko];
  return row ? row[lang === 'ja' ? 0 : 1] : ko;
}

export function useT() {
  const lang = useAtomValue(langAtom);
  return (ko: string) => translate(lang, ko);
}
