import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
// Tokens first: styles.css consumes them, and the cascade order matters so a
// token is never silently overridden by a rule.
import './styles/tokens.css';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('#root element missing from index.html');

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>
);
