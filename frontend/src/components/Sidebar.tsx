"use client";

import { useRef, useEffect, useCallback } from 'react';
import { useWorkflowStore } from '../store/useWorkflowStore';
import toast from 'react-hot-toast';

const MAX_RETRIES = 3;
const RETRY_DELAYS = [2000, 4000, 8000];

export function Sidebar() {
  const {
    steps, selectedStepId, setSelectedStepId,
    isStreaming, isSynthesizing, sessionId,
    updateStep, setIsStreaming,
    pendingStepId, setPendingStepId,
    isRunningAll, setIsRunningAll,
    addBanner,
  } = useWorkflowStore();
  const eventSourceRef = useRef<EventSource | null>(null);
  const retryCountRef = useRef<number>(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  useEffect(() => {
    return () => {
      if (eventSourceRef.current) eventSourceRef.current.close();
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, []);

  const connectStream = useCallback((stepId: number, isRetry = false) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const currentSessionId = useWorkflowStore.getState().sessionId;
    if (!currentSessionId) return;

    if (!isRetry) {
      retryCountRef.current = 0;
      setSelectedStepId(stepId);
      setIsStreaming(true);
      updateStep(stepId, { status: 'WORKING', content: '' });
    }

    const { generationMode } = useWorkflowStore.getState();
    const es = new EventSource(
      `${API_URL}/api/stream_step/${currentSessionId}/${stepId}?mode=${generationMode}`
    );
    eventSourceRef.current = es;

    es.addEventListener('chunk', (e) => {
      const data = JSON.parse(e.data);
      updateStep(stepId, {
        content: (useWorkflowStore.getState().steps.find(s => s.id === stepId)?.content || '') + data.text
      });
    });

    es.addEventListener('completed', (e) => {
      const data = JSON.parse(e.data);
      updateStep(stepId, { status: 'DONE', content: data.content });
    });

    es.addEventListener('finished', async () => {
      es.close();
      retryCountRef.current = 0;
      setIsStreaming(false);

      const stepName = useWorkflowStore.getState().steps.find(s => s.id === stepId)?.name || `${stepId}단계`;
      addBanner({ type: 'success', message: `✓ ${stepName} 완료`, stepId });

      // Run All 모드: 자동으로 synthesize 후 다음 단계 트리거
      if (useWorkflowStore.getState().isRunningAll) {
        const state = useWorkflowStore.getState();
        const content = state.steps.find(s => s.id === stepId)?.content || '';
        const nextStep = state.steps.find(s => s.id === stepId + 1);

        // synthesize로 컨텍스트 저장
        try {
          await fetch(`${API_URL}/api/step/${state.sessionId}/${stepId}/synthesize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
          });
        } catch {
          // synthesize 실패해도 계속 진행 (컨텍스트 없이 다음 단계 실행)
          addBanner({ type: 'error', message: `${stepId}단계 컨텍스트 저장 실패 — 다음 단계 계속 진행`, stepId });
        }

        if (nextStep) {
          await new Promise(resolve => setTimeout(resolve, 5000));
          setPendingStepId(nextStep.id);
        } else {
          // 마지막 단계 완료
          setIsRunningAll(false);
          addBanner({ type: 'success', message: '모든 단계 자동 완료!' });
        }
      }
    });

    es.addEventListener('error', (e) => {
      es.close();

      let serverMsg = '';
      try {
        const msg = JSON.parse((e as MessageEvent).data);
        serverMsg = msg.error || '';
      } catch {
        // connection-level error
      }

      if (serverMsg) {
        setIsStreaming(false);
        if (useWorkflowStore.getState().isRunningAll) {
          setIsRunningAll(false);
          addBanner({ type: 'error', message: `Run All 중단 — ${stepId}단계 오류: ${serverMsg}`, stepId });
        } else {
          addBanner({ type: 'error', message: `${stepId}단계 오류: ${serverMsg}`, stepId });
        }
        updateStep(stepId, { status: 'ERROR' });
        return;
      }

      if (retryCountRef.current < MAX_RETRIES) {
        const delay = RETRY_DELAYS[retryCountRef.current];
        retryCountRef.current += 1;
        toast(`연결 끊김 — ${delay / 1000}초 후 재연결 (${retryCountRef.current}/${MAX_RETRIES})`, { icon: '🔄' });
        retryTimerRef.current = setTimeout(() => connectStream(stepId, true), delay);
      } else {
        setIsStreaming(false);
        if (useWorkflowStore.getState().isRunningAll) {
          setIsRunningAll(false);
          addBanner({ type: 'error', message: `Run All 중단 — ${stepId}단계 재연결 실패`, stepId });
        } else {
          addBanner({ type: 'error', message: `${stepId}단계 재연결 실패 — 클릭하여 재시도하세요.`, stepId });
        }
        updateStep(stepId, { status: 'ERROR' });
        retryCountRef.current = 0;
      }
    });
  }, [API_URL, setIsStreaming, setIsRunningAll, setSelectedStepId, updateStep, addBanner]);

  // pendingStepId 변화 감지 → 스트림 실행 (10단계는 preview 폴링)
  useEffect(() => {
    if (pendingStepId === null) return;
    const currentSessionId = useWorkflowStore.getState().sessionId;
    if (!currentSessionId) return;
    setPendingStepId(null);

    if (pendingStepId === 10) {
      // 10단계: 백엔드에 생성 시작 요청 후 폴링
      updateStep(10, { status: 'WORKING', content: '' });
      // 명시적으로 생성 시작 요청 (캐시 없으면 백그라운드 생성 트리거)
      fetch(`${API_URL}/api/preview/${currentSessionId}/generate`, { method: 'POST' }).catch(() => {});
      const poll = async () => {
        for (let i = 0; i < 120; i++) {
          await new Promise(r => setTimeout(r, 3000));
          try {
            const res = await fetch(`${API_URL}/api/preview/${currentSessionId}/status`);
            const data = await res.json();
            if (data.status === 'ready') {
              updateStep(10, { status: 'DONE', content: '인터랙티브 데모가 준비되었습니다.' });
              setIsRunningAll(false);
              addBanner({ type: 'success', message: '모든 단계 자동 완료! Preview 버튼으로 데모를 확인하세요.', stepId: 10 });
              return;
            }
            if (data.status === 'error') {
              updateStep(10, { status: 'ERROR', content: '' });
              setIsRunningAll(false);
              addBanner({ type: 'error', message: '데모 생성 실패 — 10단계를 클릭해 재시도하세요.', stepId: 10 });
              return;
            }
          } catch { break; }
        }
        updateStep(10, { status: 'ERROR', content: '' });
        setIsRunningAll(false);
        addBanner({ type: 'error', message: '데모 생성 시간 초과 — 10단계를 클릭해 재시도하세요.', stepId: 10 });
      };
      poll();
      return;
    }

    connectStream(pendingStepId);
  }, [pendingStepId, connectStream, setPendingStepId, updateStep, setIsRunningAll, addBanner, API_URL]);

  const triggerStep = async (stepId: number) => {
    if (!sessionId) {
      toast.error("먼저 Boot System 버튼을 눌러 세션을 할당받으세요.");
      return;
    }
    // 10단계: preview 상태 확인 후 열기 or 재생성
    if (stepId === 10) {
      // ERROR/PENDING 상태면 즉시 폴링 시작 (백엔드가 아직 생성 중일 수 있음)
      const currentStep = useWorkflowStore.getState().steps.find(s => s.id === 10);
      if (currentStep?.status === 'ERROR' || currentStep?.status === 'PENDING') {
        updateStep(10, { status: 'WORKING', content: '' });
        toast('인터랙티브 데모 생성 시작! 완료까지 수분이 소요될 수 있습니다.', { icon: '🚀', duration: 5000 });
        setPendingStepId(10);
        return;
      }

      const res = await fetch(`${API_URL}/api/preview/${sessionId}/status`).catch(() => null);
      if (res?.ok) {
        const data = await res.json();
        if (data.status === 'ready') {
          // 준비됨: 열기 or 재생성 선택 (status 무관)
          toast((t) => (
              <div className="flex flex-col gap-2">
                <span className="font-semibold text-sm">인터랙티브 데모</span>
                <div className="flex gap-2">
                  <button
                    onClick={() => { window.open(`${API_URL}/api/preview/${sessionId}`, '_blank'); toast.dismiss(t.id); }}
                    className="px-3 py-1 bg-violet-600 hover:bg-violet-500 text-white rounded text-xs font-bold"
                  >
                    🖥 열기
                  </button>
                  <button
                    onClick={() => {
                      toast.dismiss(t.id);
                      // 요구사항 입력 토스트
                      toast((t2) => {
                        let reqText = '';
                        return (
                          <div className="flex flex-col gap-2" style={{ minWidth: 260 }}>
                            <span className="font-semibold text-sm">재생성 요구사항 (선택)</span>
                            <textarea
                              rows={3}
                              placeholder="예: 차트를 추가해줘, 모바일 레이아웃으로..."
                              className="w-full text-xs rounded p-2 bg-neutral-800 border border-neutral-600 text-neutral-100 resize-none focus:outline-none focus:border-violet-500"
                              onChange={(e) => { reqText = e.target.value; }}
                              style={{ fontFamily: 'inherit' }}
                            />
                            <div className="flex gap-2 justify-end">
                              <button
                                onClick={() => toast.dismiss(t2.id)}
                                className="px-3 py-1 bg-neutral-700 hover:bg-neutral-600 text-white rounded text-xs"
                              >
                                취소
                              </button>
                              <button
                                onClick={async () => {
                                  toast.dismiss(t2.id);
                                  await fetch(`${API_URL}/api/preview/${sessionId}/regenerate`, {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ user_requirements: reqText }),
                                  });
                                  updateStep(10, { status: 'WORKING', content: '' });
                                  toast('데모 재생성 시작...', { icon: '🔄' });
                                  setPendingStepId(10);
                                }}
                                className="px-3 py-1 bg-violet-600 hover:bg-violet-500 text-white rounded text-xs font-bold"
                              >
                                🔄 재생성
                              </button>
                            </div>
                          </div>
                        );
                      }, { duration: 30000 });
                    }}
                    className="px-3 py-1 bg-neutral-700 hover:bg-neutral-600 text-white rounded text-xs font-bold"
                  >
                    🔄 재생성
                  </button>
                </div>
              </div>
            ), { duration: 8000 });
        } else {
          toast('아직 생성 중입니다. 잠시 후 다시 눌러주세요.', { icon: '⏳' });
        }
      } else {
        window.open(`${API_URL}/api/preview/${sessionId}`, '_blank');
      }
      return;
    }
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    connectStream(stepId);
  };

  return (
    <aside className="w-80 border-r border-neutral-800 bg-neutral-900/50 flex flex-col overflow-y-auto shrink-0">
      <div className="p-4 uppercase text-xs font-bold text-neutral-500 tracking-wider flex justify-between items-center border-b border-neutral-800/50">
        <span>Agent Directed Graph</span>
        {sessionId && (
          <span className="flex items-center gap-2">
            {isRunningAll && <span className="text-amber-400 text-[10px] animate-pulse">AUTO</span>}
            <span className="text-rose-500 animate-pulse text-[10px]">LIVE</span>
          </span>
        )}
      </div>
      <div className="flex flex-col p-2 space-y-1">
        {steps.length === 0 && (
          <div className="text-center text-neutral-600 text-sm mt-10">상단에서 세션을 시작하세요</div>
        )}
        {(() => {
          const doneCount = steps.filter(s => s.status === 'DONE').length;
          const resumeStep = steps.find(s => s.status !== 'DONE');
          const isStep10Working = steps.find(s => s.id === 10)?.status === 'WORKING';
          const hasPartial = doneCount > 0 && doneCount < steps.length && !isRunningAll && !isStreaming && !isStep10Working;

          return (
            <>
              {hasPartial && (
                <div className="mx-1 mb-2 px-3 py-2 rounded-lg bg-amber-950/40 border border-amber-800/50 text-amber-300 text-xs flex items-start gap-2">
                  <span className="shrink-0 mt-0.5">⏸</span>
                  <div>
                    <div className="font-semibold mb-0.5">{doneCount}/{steps.length}단계 완료 — 중단됨</div>
                    <div className="text-amber-400/70">
                      {resumeStep ? `"${resumeStep.name}"부터 이어서 실행하려면 Run All을 누르세요.` : ''}
                    </div>
                  </div>
                </div>
              )}
              {steps.map((step, idx) => {
                const isResumePoint = hasPartial && step.id === resumeStep?.id;
                return (
                  <div key={step.id}>
                    {isResumePoint && (
                      <div className="flex items-center gap-2 px-3 py-1 text-[10px] text-amber-500/70 uppercase tracking-wider">
                        <div className="flex-1 h-px bg-amber-800/40" />
                        <span>여기서 재개</span>
                        <div className="flex-1 h-px bg-amber-800/40" />
                      </div>
                    )}
                    <button
                      onClick={() => triggerStep(step.id)}
                      disabled={!sessionId || ((isStreaming || isSynthesizing || isRunningAll) && step.id !== 10)}
                      className={`w-full text-left text-sm px-3 py-3 rounded-lg flex items-start gap-3 transition
                        ${selectedStepId === step.id ? 'bg-neutral-800 text-white ring-1 ring-neutral-700' : 'text-neutral-400 hover:bg-neutral-800/50'}
                        ${(isStreaming || isSynthesizing || isRunningAll) && selectedStepId !== step.id ? 'opacity-50 cursor-not-allowed' : ''}
                        ${step.status === 'DONE' && hasPartial ? 'opacity-60' : ''}
                      `}
                    >
                      <div className="mt-0.5 shrink-0">
                        {step.status === 'DONE' && <div className="w-2 h-2 rounded-full bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]" />}
                        {step.status === 'WORKING' && <div className="w-2 h-2 rounded-full bg-blue-500 animate-pulse shadow-[0_0_8px_rgba(59,130,246,0.6)]" />}
                        {step.status === 'PENDING' && <div className="w-2 h-2 rounded-full bg-neutral-700" />}
                        {step.status === 'ERROR' && <div className="w-2 h-2 rounded-full bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.6)]" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <span className="leading-snug">{step.name}</span>
                        {step.status === 'DONE' && (
                          <span className="ml-2 text-[10px] text-green-500/60">완료</span>
                        )}
                        {step.status === 'ERROR' && (
                          <span className="ml-2 text-[10px] text-red-400/80">오류 — 재시도</span>
                        )}
                      </div>
                      {isResumePoint && !isStreaming && !isRunningAll && (
                        <span className="shrink-0 text-[10px] text-amber-400 font-semibold">재개 ▶</span>
                      )}
                    </button>
                  </div>
                );
              })}
            </>
          );
        })()}
      </div>
    </aside>
  );
}
