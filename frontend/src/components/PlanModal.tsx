"use client";

import { useState } from 'react';
import toast from 'react-hot-toast';

interface Page {
  name: string;
  description: string;
}

interface PlanModalProps {
  sessionId: string;
  domain: string;
  apiUrl: string;
  onClose: () => void;
}

type ModalStep = 'idle' | 'loading_pages' | 'edit_pages' | 'generating' | 'done';

export function PlanModal({ sessionId, domain, apiUrl, onClose }: PlanModalProps) {
  const [step, setStep] = useState<ModalStep>('idle');
  const [pages, setPages] = useState<Page[]>([]);
  const [comment, setComment] = useState('');
  const [newPageName, setNewPageName] = useState('');
  const [progress, setProgress] = useState({ current: 0, total: 0, currentName: '' });
  const [markdownResult, setMarkdownResult] = useState('');
  const [generateMode, setGenerateMode] = useState<'all' | null>(null);

  const loadPages = async () => {
    setStep('loading_pages');
    try {
      const res = await fetch(`${apiUrl}/api/plan/pages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '페이지 목록 생성 실패');
      }
      const data = await res.json();
      setPages(data.pages);
      setStep('edit_pages');
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '페이지 목록 생성 실패');
      setStep('idle');
    }
  };

  const revisePagesWithComment = async () => {
    if (!comment.trim()) return;
    setStep('loading_pages');
    try {
      const res = await fetch(`${apiUrl}/api/plan/pages/revise`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, pages, comment }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '재생성 실패');
      }
      const data = await res.json();
      setPages(data.pages);
      setComment('');
      setStep('edit_pages');
      toast.success('페이지 목록이 재생성되었습니다.');
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '재생성 실패');
      setStep('edit_pages');
    }
  };

  const deletePage = (index: number) => {
    setPages(prev => prev.filter((_, i) => i !== index));
  };

  const addPage = () => {
    if (!newPageName.trim()) return;
    setPages(prev => [...prev, { name: newPageName.trim(), description: '' }]);
    setNewPageName('');
  };

  const generateAll = async () => {
    setGenerateMode('all');
    setStep('generating');
    setProgress({ current: 0, total: pages.length, currentName: '' });

    let fullMarkdown = `# ${domain} 서비스 기획서\n\n`;
    fullMarkdown += `> 생성일: ${new Date().toLocaleDateString('ko-KR')}\n\n---\n\n`;

    for (let i = 0; i < pages.length; i++) {
      const page = pages[i];
      setProgress({ current: i + 1, total: pages.length, currentName: page.name });

      try {
        const res = await fetch(`${apiUrl}/api/plan/generate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            page_name: page.name,
            mode: 'single',
          }),
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || `${page.name} 생성 실패`);
        }
        const data = await res.json();
        fullMarkdown += data.content + '\n\n---\n\n';
      } catch (e: unknown) {
        toast.error(e instanceof Error ? e.message : `${page.name} 생성 실패`);
        fullMarkdown += `## ${page.name}\n\n> 생성 실패\n\n---\n\n`;
      }
    }

    setMarkdownResult(fullMarkdown);
    setStep('done');
  };

  const generateSingle = async (page: Page) => {
    setGenerateMode(null);
    setStep('generating');
    setProgress({ current: 1, total: 1, currentName: page.name });

    try {
      const res = await fetch(`${apiUrl}/api/plan/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          page_name: page.name,
          mode: 'single',
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '생성 실패');
      }
      const data = await res.json();
      const md = `# ${domain} — ${page.name} 기획서\n\n---\n\n${data.content}`;
      setMarkdownResult(md);
      setStep('done');
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '생성 실패');
      setStep('edit_pages');
    }
  };

  const downloadMarkdown = () => {
    const blob = new Blob([markdownResult], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${domain}_기획서.md`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success('기획서 다운로드 완료');
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="bg-neutral-900 border border-neutral-700 rounded-xl shadow-2xl w-full max-w-2xl mx-4 max-h-[85vh] flex flex-col">
        {/* 헤더 */}
        <div className="flex items-center justify-between p-5 border-b border-neutral-800 shrink-0">
          <div>
            <h2 className="text-white font-bold text-lg">기획서 생성</h2>
            <p className="text-neutral-500 text-xs mt-0.5">{domain}</p>
          </div>
          <button
            onClick={onClose}
            className="text-neutral-500 hover:text-white transition text-xl"
          >
            ✕
          </button>
        </div>

        {/* 본문 */}
        <div className="flex-1 overflow-y-auto p-5">

          {/* 초기 상태 */}
          {step === 'idle' && (
            <div className="flex flex-col items-center justify-center py-12 gap-4">
              <div className="text-4xl">📋</div>
              <p className="text-neutral-400 text-sm text-center">
                워크플로우 결과를 바탕으로 서비스 기획서를 생성합니다.<br />
                먼저 AI가 페이지 목록을 분석합니다.
              </p>
              <button
                onClick={loadPages}
                className="bg-indigo-600 hover:bg-indigo-500 text-white px-6 py-2.5 rounded-lg font-semibold transition"
              >
                페이지 목록 분석 시작
              </button>
            </div>
          )}

          {/* 로딩 */}
          {step === 'loading_pages' && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <div className="text-3xl animate-spin">⚙</div>
              <p className="text-neutral-400 text-sm">AI가 페이지 목록을 분석 중...</p>
            </div>
          )}

          {/* 페이지 편집 */}
          {step === 'edit_pages' && (
            <div className="flex flex-col gap-4">
              <p className="text-neutral-400 text-xs">페이지를 추가/삭제하거나 코멘트로 재생성할 수 있습니다.</p>

              {/* 페이지 목록 */}
              <div className="flex flex-col gap-2">
                {pages.map((page, i) => (
                  <div key={i} className="flex items-center gap-3 bg-neutral-800 rounded-lg px-4 py-3">
                    <span className="text-neutral-500 text-xs w-5 shrink-0">{i + 1}</span>
                    <div className="flex-1 min-w-0">
                      <div className="text-white text-sm font-medium">{page.name}</div>
                      {page.description && (
                        <div className="text-neutral-500 text-xs mt-0.5 truncate">{page.description}</div>
                      )}
                    </div>
                    <button
                      onClick={() => generateSingle(page)}
                      className="text-indigo-400 hover:text-indigo-300 text-xs shrink-0 px-2 py-1 rounded hover:bg-indigo-900/30 transition"
                      title="이 페이지만 생성"
                    >
                      단독 생성
                    </button>
                    <button
                      onClick={() => deletePage(i)}
                      className="text-neutral-600 hover:text-red-400 transition text-sm shrink-0"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>

              {/* 페이지 추가 */}
              <div className="flex gap-2">
                <input
                  type="text"
                  value={newPageName}
                  onChange={e => setNewPageName(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && addPage()}
                  placeholder="새 페이지 이름 추가..."
                  className="flex-1 bg-neutral-800 border border-neutral-700 text-white rounded px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-indigo-500"
                />
                <button
                  onClick={addPage}
                  className="bg-neutral-700 hover:bg-neutral-600 text-white px-3 py-2 rounded text-sm transition"
                >
                  + 추가
                </button>
              </div>

              {/* 코멘트 재생성 */}
              <div className="border-t border-neutral-800 pt-4 flex flex-col gap-2">
                <label className="text-neutral-500 text-xs">코멘트로 전체 재생성</label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={comment}
                    onChange={e => setComment(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && revisePagesWithComment()}
                    placeholder="예: 관리자 페이지 추가하고 로그인 페이지는 빼줘"
                    className="flex-1 bg-neutral-800 border border-neutral-700 text-white rounded px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                  <button
                    onClick={revisePagesWithComment}
                    disabled={!comment.trim()}
                    className="bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40 text-white px-3 py-2 rounded text-sm transition shrink-0"
                  >
                    재생성
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* 생성 중 */}
          {step === 'generating' && (
            <div className="flex flex-col items-center justify-center py-12 gap-4">
              <div className="text-3xl animate-spin">⚙</div>
              <p className="text-white font-medium">
                {progress.currentName} 기획서 작성 중...
              </p>
              {progress.total > 1 && (
                <>
                  <p className="text-neutral-500 text-sm">
                    {progress.current} / {progress.total} 페이지
                  </p>
                  <div className="w-64 bg-neutral-800 rounded-full h-2">
                    <div
                      className="bg-indigo-500 h-2 rounded-full transition-all"
                      style={{ width: `${(progress.current / progress.total) * 100}%` }}
                    />
                  </div>
                </>
              )}
            </div>
          )}

          {/* 완료 */}
          {step === 'done' && (
            <div className="flex flex-col gap-4">
              <div className="flex items-center gap-2 text-emerald-400">
                <span>✓</span>
                <span className="font-medium text-sm">기획서 생성 완료</span>
              </div>
              <pre className="bg-neutral-800 rounded-lg p-4 text-neutral-300 text-xs leading-relaxed overflow-auto max-h-80 whitespace-pre-wrap font-mono">
                {markdownResult}
              </pre>
            </div>
          )}
        </div>

        {/* 푸터 */}
        <div className="p-5 border-t border-neutral-800 shrink-0 flex justify-between items-center">
          {step === 'done' ? (
            <>
              <button
                onClick={() => { setStep('edit_pages'); setMarkdownResult(''); }}
                className="text-neutral-500 hover:text-white text-sm transition"
              >
                ← 다시 편집
              </button>
              <button
                onClick={downloadMarkdown}
                className="bg-emerald-600 hover:bg-emerald-500 text-white px-5 py-2 rounded-lg font-semibold transition"
              >
                .md 다운로드
              </button>
            </>
          ) : step === 'edit_pages' ? (
            <>
              <span className="text-neutral-600 text-xs">{pages.length}개 페이지</span>
              <button
                onClick={generateAll}
                disabled={pages.length === 0}
                className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white px-5 py-2 rounded-lg font-semibold transition"
              >
                전체 한번에 생성
              </button>
            </>
          ) : (
            <div />
          )}
        </div>
      </div>
    </div>
  );
}
