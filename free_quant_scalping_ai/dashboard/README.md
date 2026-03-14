# Live dashboard

React UI for **Free Quant Scalping AI**. Shows live NIFTY/SENSEX prices, scalping signals, hero-zero, institutional flow, gamma & expiry.

## Run

1. Start the **backend** first (from project root):
   ```bash
   .\.venv\Scripts\python.exe main.py
   ```
   Or with port: `$env:PORT=8001; .\.venv\Scripts\python.exe main.py`

2. Start the **dashboard**:
   ```bash
   cd dashboard/react_dashboard
   npm install
   npm run dev
   ```

3. Open **http://localhost:3000** in your browser.

The dashboard polls the API every 3 seconds. Set `VITE_API_URL` if your backend runs on a different host/port (e.g. `http://localhost:8001`).
