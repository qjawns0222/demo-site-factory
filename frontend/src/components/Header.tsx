"use client";

import { useState, useRef, useEffect } from 'react';
import { useWorkflowStore } from '../store/useWorkflowStore';
import toast from 'react-hot-toast';
import { PlanModal } from './PlanModal';

export function Header() {
  const {
    domain, sessionId, setDomain, setSessionId, setSteps,
    isStreaming, isSynthesizing, steps, resetAll,
    isRunningAll, setIsRunningAll, setPendingStepId,
  } = useWorkflowStore();
  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  const [sessions, setSessions] = useState<{session_id: string; domain: string}[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [isRestoring, setIsRestoring] = useState(false);
  const [showPlanModal, setShowPlanModal] = useState(false);
  const historyRef = useRef<HTMLDivElement>(null);

  // sessionId가 바뀌면 localStorage에 저장
  useEffect(() => {
    if (sessionId) {
      localStorage.setItem('demo_session_id', sessionId);
      localStorage.setItem('demo_domain', domain);
    }
  }, [sessionId, domain]);

  // planPages가 바뀌면 localStorage에 저장
  const { planPages, setPlanPages, planResult, setPlanResult, planResultMap, setPlanResultMap, generatedPageNames, addGeneratedPageName } = useWorkflowStore();
  useEffect(() => {
    if (sessionId) {
      localStorage.setItem(`demo_plan_pages_${sessionId}`, JSON.stringify(planPages));
    }
  }, [planPages, sessionId]);
  useEffect(() => {
    if (sessionId) {
      localStorage.setItem(`demo_plan_result_${sessionId}`, planResult);
    }
  }, [planResult, sessionId]);
  useEffect(() => {
    if (sessionId) {
      localStorage.setItem(`demo_plan_generated_${sessionId}`, JSON.stringify(generatedPageNames));
    }
  }, [generatedPageNames, sessionId]);
  useEffect(() => {
    if (sessionId) {
      localStorage.setItem(`demo_plan_result_map_${sessionId}`, JSON.stringify(planResultMap));
    }
  }, [planResultMap, sessionId]);

  // 페이지 로드 시 localStorage에서 세션 복구
  useEffect(() => {
    const savedId = localStorage.getItem('demo_session_id');
    const savedDomain = localStorage.getItem('demo_domain');
    if (!savedId || !savedDomain) return;

    setIsRestoring(true);
    (async () => {
      try {
        const res = await fetch(`${API_URL}/api/session/${savedId}/steps`);
        if (!res.ok) {
          localStorage.removeItem('demo_session_id');
          localStorage.removeItem('demo_domain');
          return;
        }
        const data = await res.json();

        const wfRes = await fetch(`${API_URL}/api/workflow`);
        const wfData = await wfRes.json();
        if (!wfData.steps) return;

        const restoredSteps = wfData.steps.map((s: any) => ({
          ...s,
          status: data.steps[s.id] ? 'DONE' : 'PENDING',
          content: data.steps[s.id] || '',
        }));

        setSteps(restoredSteps);
        setDomain(savedDomain);
        setSessionId(savedId);

        // planPages 복구
        const savedPages = localStorage.getItem(`demo_plan_pages_${savedId}`);
        if (savedPages) {
          try { setPlanPages(JSON.parse(savedPages)); } catch { /* 무시 */ }
        }
        // planResult 복구
        const savedResult = localStorage.getItem(`demo_plan_result_${savedId}`);
        if (savedResult) setPlanResult(savedResult);
        // generatedPageNames 복구
        const savedGenerated = localStorage.getItem(`demo_plan_generated_${savedId}`);
        if (savedGenerated) {
          try { JSON.parse(savedGenerated).forEach((n: string) => addGeneratedPageName(n)); } catch { /* 무시 */ }
        }
        // planResultMap 복구
        const savedResultMap = localStorage.getItem(`demo_plan_result_map_${savedId}`);
        if (savedResultMap) {
          try { setPlanResultMap(JSON.parse(savedResultMap)); } catch { /* 무시 */ }
        }

        const doneCount = restoredSteps.filter((s: any) => s.status === 'DONE').length;
        if (doneCount > 0) {
          toast(`세션 복구됨: ${savedDomain} (${doneCount}단계 완료)`, { icon: '🔄' });
        }
      } catch {
        // 복구 실패 시 무시
      } finally {
        setIsRestoring(false);
      }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 히스토리 드롭다운 외부 클릭 시 닫기
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (historyRef.current && !historyRef.current.contains(e.target as Node)) {
        setShowHistory(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const fetchWorkflow = async () => {
    try {
      const res = await fetch(`${API_URL}/api/workflow`);
      const data = await res.json();
      if (data.steps) {
        setSteps(data.steps.map((s: any) => ({ ...s, status: 'PENDING', content: '' })));
      }
    } catch {
      toast.error("서버에서 워크플로우를 가져오는데 실패했습니다.");
    }
  };

  const fetchSessions = async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions`);
      const data = await res.json();
      setSessions(data.sessions || []);
    } catch {
      toast.error("세션 히스토리를 불러오는데 실패했습니다.");
    }
  };

  const resetGeneration = async () => {
    if (!sessionId) return toast.error('활성화된 세션이 없습니다.');
    if (!confirm('정말로 로컬 세션을 완전히 삭제합니까? 데이터는 복구할 수 없습니다.')) return;

    try {
      const res = await fetch(`${API_URL}/api/session/${sessionId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('API 오류');
      localStorage.removeItem(`demo_plan_pages_${sessionId}`);
      localStorage.removeItem(`demo_plan_result_${sessionId}`);
      localStorage.removeItem(`demo_plan_generated_${sessionId}`);
      localStorage.removeItem(`demo_plan_result_map_${sessionId}`);
      resetAll();
      localStorage.removeItem('demo_session_id');
      localStorage.removeItem('demo_domain');
      toast.success('세션이 완전 초기화되었습니다.');
      fetchWorkflow();
    } catch {
      toast.error('초기화 중 오류 발생');
    }
  };

  const startGeneration = async () => {
    if (!domain.trim()) return toast.error('도메인을 입력해주세요.');
    if (steps.length === 0) {
      await fetchWorkflow();
    }

    try {
      const req = await fetch(`${API_URL}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ domain: domain.trim() })
      });
      if (!req.ok) {
        const errorData = await req.json();
        throw new Error(errorData.detail || 'API 호출 실패');
      }
      const data = await req.json();
      setSessionId(data.session_id);
      toast.success(`세션 할당 완료: ${data.session_id.slice(0, 8)}...`);
    } catch (err: any) {
      toast.error(`개시 실패: ${err.message}`);
    }
  };

  const runAll = async () => {
    if (!sessionId || steps.length === 0) return;
    if (isStreaming || isSynthesizing) return;

    // 완료되지 않은 첫 번째 단계부터 시작 (DONE 이후 재개 지원)
    const firstPending = steps.find(s => s.status !== 'DONE');
    if (!firstPending) {
      toast('모든 단계가 이미 완료되어 있습니다.', { icon: '✅' });
      return;
    }

    const doneCount = steps.filter(s => s.status === 'DONE').length;
    const resumeMsg = doneCount > 0
      ? `${doneCount}단계 완료됨 — ${firstPending.id}단계부터 이어서 실행합니다...`
      : '전체 자동 실행을 시작합니다...';

    setIsRunningAll(true);
    toast(resumeMsg, { icon: doneCount > 0 ? '▶️' : '🚀' });
    setPendingStepId(firstPending.id);
  };

  const restoreSession = async (sess: {session_id: string; domain: string}) => {
    if (isStreaming || isSynthesizing) {
      toast.error('진행 중인 작업이 있습니다. 완료 후 복원하세요.');
      return;
    }
    if (sessionId && !confirm('현재 세션을 버리고 선택한 세션으로 복원하시겠습니까?')) return;

    resetAll();
    setDomain(sess.domain);
    setShowHistory(false);

    try {
      const [wfRes, stepsRes] = await Promise.all([
        fetch(`${API_URL}/api/workflow`),
        fetch(`${API_URL}/api/session/${sess.session_id}/steps`),
      ]);
      const wfData = await wfRes.json();
      const stepsData = stepsRes.ok ? await stepsRes.json() : { steps: {} };

      if (wfData.steps) {
        const restoredSteps = wfData.steps.map((s: any) => ({
          ...s,
          status: stepsData.steps[s.id] ? 'DONE' : 'PENDING',
          content: stepsData.steps[s.id] || '',
        }));
        setSteps(restoredSteps);
        const doneCount = restoredSteps.filter((s: any) => s.status === 'DONE').length;
        toast.success(`세션 복원: ${sess.domain}${doneCount > 0 ? ` (${doneCount}단계 완료)` : ''}`);
      }
    } catch {
      toast.error('세션 복원 중 오류 발생');
    }

    setSessionId(sess.session_id);
    localStorage.setItem('demo_session_id', sess.session_id);
    localStorage.setItem('demo_domain', sess.domain);

    // planPages 복구
    const savedPages = localStorage.getItem(`demo_plan_pages_${sess.session_id}`);
    if (savedPages) {
      try { setPlanPages(JSON.parse(savedPages)); } catch { /* 무시 */ }
    } else {
      setPlanPages([]);
    }
    // planResult 복구
    const savedResult = localStorage.getItem(`demo_plan_result_${sess.session_id}`);
    setPlanResult(savedResult ?? '');
    // generatedPageNames 복구
    const savedGenerated = localStorage.getItem(`demo_plan_generated_${sess.session_id}`);
    if (savedGenerated) {
      try { JSON.parse(savedGenerated).forEach((n: string) => addGeneratedPageName(n)); } catch { /* 무시 */ }
    }
    // planResultMap 복구
    const savedResultMap = localStorage.getItem(`demo_plan_result_map_${sess.session_id}`);
    if (savedResultMap) {
      try { setPlanResultMap(JSON.parse(savedResultMap)); } catch { /* 무시 */ }
    }
  };

  const handleExportZip = () => {
    if (!sessionId) return toast.error('다운로드할 세션 데이터가 없습니다.');
    window.location.href = `${API_URL}/api/export/${sessionId}`;
  };

  const handlePreview = () => {
    if (!sessionId) return toast.error('프리뷰할 세션 데이터가 없습니다.');
    window.open(`${API_URL}/api/preview/${sessionId}`, '_blank');
  };

  const handlePreviewSource = async () => {
    if (!sessionId) return toast.error('세션 데이터가 없습니다.');
    try {
      const res = await fetch(`${API_URL}/api/preview/${sessionId}/source`);
      if (!res.ok) {
        const err = await res.json();
        return toast.error(err.detail || '소스코드를 불러올 수 없습니다.');
      }
      const data = await res.json();
      const blob = new Blob([data.html], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${sessionId.slice(0, 8)}-demo.html`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success('HTML 소스 다운로드 완료');
    } catch {
      toast.error('소스코드 다운로드 실패');
    }
  };

  const isBusy = isStreaming || isSynthesizing || isRunningAll || isRestoring;

  return (
    <header className="border-b border-neutral-800 bg-neutral-900 p-4 shrink-0">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        {/* 로고 */}
        <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2 shrink-0">
          <span className="bg-rose-700 text-white px-2 py-0.5 rounded text-sm shadow-md shadow-rose-900/50">V8</span>
          AI Demo Site Factory
        </h1>

        {/* 메인 컨트롤 */}
        <div className="flex gap-2 flex-1 min-w-0 flex-wrap">
          {/* 도메인 입력 */}
          <input
            type="text"
            className="flex-1 min-w-[160px] bg-neutral-800 border border-neutral-700 text-white rounded px-3 py-2 outline-none focus:ring-2 focus:ring-rose-500 font-mono text-sm"
            placeholder="도메인 주제 입력 (예: chat-service)"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            disabled={isBusy || !!sessionId}
          />

          {/* 세션 히스토리 */}
          <div className="relative shrink-0" ref={historyRef}>
            <button
              onClick={() => { fetchSessions(); setShowHistory(v => !v); }}
              className="bg-neutral-800 border border-neutral-700 hover:bg-neutral-700 text-neutral-300 px-3 py-2 rounded text-sm transition"
              title="세션 히스토리"
            >
              🕐
            </button>
            {showHistory && (
              <div className="absolute right-0 top-full mt-1 w-64 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl z-50 max-h-64 overflow-y-auto">
                <div className="p-2 text-[10px] uppercase tracking-wider text-neutral-500 border-b border-neutral-800">최근 세션</div>
                {sessions.length === 0 ? (
                  <div className="p-4 text-neutral-600 text-sm text-center">세션 없음</div>
                ) : (
                  sessions.map(sess => (
                    <button
                      key={sess.session_id}
                      onClick={() => restoreSession(sess)}
                      className="w-full text-left px-3 py-2 hover:bg-neutral-800 transition text-sm"
                    >
                      <div className="text-white font-mono">{sess.domain}</div>
                      <div className="text-neutral-500 text-[11px]">{sess.session_id.slice(0, 12)}...</div>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          {/* Boot / New Session / Reset */}
          {sessionId ? (
            <>
              <button
                onClick={() => {
                  // 현재 세션은 히스토리에 남기고 UI만 초기화
                  localStorage.removeItem(`demo_plan_pages_${sessionId}`);
                  localStorage.removeItem(`demo_plan_result_${sessionId}`);
                  localStorage.removeItem(`demo_plan_generated_${sessionId}`);
                  localStorage.removeItem(`demo_plan_result_map_${sessionId}`);
                  resetAll();
                  localStorage.removeItem('demo_session_id');
                  localStorage.removeItem('demo_domain');
                  fetchWorkflow();
                  toast('새 세션을 시작합니다. 도메인을 입력하고 Boot 하세요.', { icon: '✨' });
                }}
                disabled={isBusy}
                className="bg-blue-800/40 text-blue-400 border border-blue-700/50 hover:bg-blue-800/80 px-3 py-2 rounded font-bold transition disabled:opacity-50 text-sm shrink-0"
              >
                + 새 세션
              </button>
              <button
                onClick={resetGeneration}
                disabled={isBusy}
                className="bg-red-900/30 text-red-500 border border-red-800/50 hover:bg-red-900/80 px-4 py-2 rounded font-bold transition disabled:opacity-50 text-sm shrink-0"
              >
                Reset
              </button>
            </>
          ) : (
            <button
              onClick={startGeneration}
              disabled={isBusy || !domain.trim()}
              className="bg-rose-600 hover:bg-rose-500 text-white px-4 py-2 rounded font-semibold transition disabled:opacity-50 shrink-0 text-sm"
            >
              Boot
            </button>
          )}

          {/* Run All */}
          {sessionId && (
            <button
              onClick={runAll}
              disabled={isBusy}
              className="bg-amber-600 hover:bg-amber-500 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-4 py-2 rounded font-semibold transition shrink-0 text-sm flex items-center gap-1"
            >
              {isRunningAll ? <span className="animate-spin">⚙</span> : '🚀'}
              {isRunningAll ? '실행 중...' : 'Run All'}
            </button>
          )}

          {/* 프리뷰 */}
          <button
            onClick={handlePreview}
            disabled={!sessionId}
            className="bg-violet-700 hover:bg-violet-600 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-4 py-2 rounded font-semibold transition shrink-0 text-sm"
          >
            🖥 Preview
          </button>

          {/* HTML 소스 다운로드 */}
          <button
            onClick={handlePreviewSource}
            disabled={!sessionId}
            className="bg-neutral-700 hover:bg-neutral-600 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-3 py-2 rounded font-semibold transition shrink-0 text-sm"
            title="HTML 소스코드 다운로드"
          >
            &lt;/&gt;
          </button>

          {/* ZIP Export */}
          <button
            onClick={handleExportZip}
            disabled={!sessionId}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-4 py-2 rounded font-semibold transition shrink-0 text-sm"
          >
            ZIP
          </button>

          {/* 기획서 생성 */}
          <button
            onClick={() => setShowPlanModal(true)}
            disabled={!sessionId}
            className="bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-4 py-2 rounded font-semibold transition shrink-0 text-sm"
          >
            📋 기획서
          </button>
        </div>
      </div>

      {/* 기획서 모달 */}
      {showPlanModal && sessionId && (
        <PlanModal
          sessionId={sessionId}
          domain={domain}
          apiUrl={API_URL}
          onClose={() => setShowPlanModal(false)}
        />
      )}

      {/* 복구 중 표시 */}
      {isRestoring && (
        <div className="mt-2 text-[11px] text-amber-400 animate-pulse">⏳ 이전 세션 복구 중...</div>
      )}

      {/* 자동 실행 중 표시 */}
      {sessionId && !isRestoring && isRunningAll && (
        <div className="mt-2 flex items-center gap-2 text-[11px] text-neutral-500">
          <span className="text-amber-400 animate-pulse">● 자동 실행 중</span>
        </div>
      )}
    </header>
  );
}
