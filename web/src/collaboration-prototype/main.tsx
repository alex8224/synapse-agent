import { createRoot } from 'react-dom/client';
import '../index.css';
import { Experience } from './Experience.tsx';

document.documentElement.dataset.theme = 'fluent-light';
createRoot(document.getElementById('root')!).render(<Experience />);
