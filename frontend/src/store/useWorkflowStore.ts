import { create } from 'zustand';

export type StepState = {
  id: number;
  name: string;
  status: 'PENDING' | 'WORKING' | 'DONE' | 'ERROR';
  content?: string;
};

export type StatusBanner = {
  type: 'success' | 'error' | 'info';
  message: string;
  stepId?: number;
};

interface WorkflowState {
  domain: string;
  sessionId: string;
  steps: StepState[];
  selectedStepId: number | null;
  isStreaming: boolean;
  isSynthesizing: boolean;
  pendingStepId: number | null;
  isRunningAll: boolean;
  generationMode: 'doc' | 'code';
  statusBanners: StatusBanner[];

  setDomain: (domain: string) => void;
  setSessionId: (id: string) => void;
  setSteps: (steps: StepState[]) => void;
  updateStep: (id: number, data: Partial<StepState>) => void;
  setSelectedStepId: (id: number | null) => void;
  setIsStreaming: (val: boolean) => void;
  setIsSynthesizing: (val: boolean) => void;
  setPendingStepId: (id: number | null) => void;
  setIsRunningAll: (val: boolean) => void;
  setGenerationMode: (mode: 'doc' | 'code') => void;
  addBanner: (banner: StatusBanner) => void;
  dismissBanner: (index: number) => void;
  clearBanners: () => void;
  resetAll: () => void;
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
  domain: '',
  sessionId: '',
  steps: [],
  selectedStepId: null,
  isStreaming: false,
  isSynthesizing: false,
  pendingStepId: null,
  isRunningAll: false,
  generationMode: 'doc',
  statusBanners: [],

  setDomain: (domain) => set({ domain }),
  setSessionId: (id) => set({ sessionId: id }),
  setSteps: (steps) => set({ steps }),
  updateStep: (id, data) =>
    set((state) => ({
      steps: state.steps.map(s => s.id === id ? { ...s, ...data } : s)
    })),
  setSelectedStepId: (id) => set({ selectedStepId: id }),
  setIsStreaming: (val) => set({ isStreaming: val }),
  setIsSynthesizing: (val) => set({ isSynthesizing: val }),
  setPendingStepId: (id) => set({ pendingStepId: id }),
  setIsRunningAll: (val) => set({ isRunningAll: val }),
  setGenerationMode: (mode) => set({ generationMode: mode }),
  addBanner: (banner) => set((state) => ({ statusBanners: [...state.statusBanners, banner] })),
  dismissBanner: (index) => set((state) => ({ statusBanners: state.statusBanners.filter((_, i) => i !== index) })),
  clearBanners: () => set({ statusBanners: [] }),
  resetAll: () => set({
    steps: [],
    selectedStepId: null,
    sessionId: '',
    domain: '',
    pendingStepId: null,
    isRunningAll: false,
    statusBanners: [],
  }),
}));
