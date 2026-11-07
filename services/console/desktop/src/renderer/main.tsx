import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles.css';

try { document.documentElement.dataset.reducedMotion = localStorage.getItem('sova.reducedMotion') ?? 'false'; } catch { /* default motion */ }
const client = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, refetchOnWindowFocus: false } } });
createRoot(document.getElementById('root')!).render(
  <StrictMode><QueryClientProvider client={client}><App /></QueryClientProvider></StrictMode>,
);
