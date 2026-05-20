# ComplianceCartridge

An offline, AI-powered compliance analysis engine designed to evaluate your organization's documents against industry standards (such as ISO 27001, GDPR, and SOC 2). By leveraging a local Retrieval-Augmented Generation (RAG) architecture, it produces comprehensive, audit-ready PDF reports without ever sending sensitive data to external servers.

## Key Capabilities

- **Strict Data Privacy**: All document processing, vector embeddings, and LLM inferences occur on your local hardware.
- **Automated Gap Analysis**: Analyzes documents against specific regulatory controls to determine compliance levels (Compliant, Partial, Non-Compliant).
- **Executive Summaries**: Generates multilingual, professional summaries of the audit findings.
- **Professional Deliverables**: Compiles findings into an institutional-grade, color-coded PDF report using LaTeX.

## Architecture

```mermaid
graph LR
    Client[Frontend<br>React 19 · Vite]

    subgraph Backend [Backend API · FastAPI :8000]
        Status[GET  /api/v1/status]
        Ingest[POST /api/v1/ingest]
        Audit[POST /api/v1/audit]
        Summary[POST /api/v1/summary]
        Export[POST /api/v1/export/pdf]
        Clear[POST /api/v1/clear_db]
    end

    subgraph Services [Local Services]
        Ollama[Ollama<br>llama3.1:8b · nomic-embed-text]
        Chroma[(ChromaDB<br>Vector Store)]
        Pdflatex{{pdflatex · TeX Live}}
    end

    Client -->|HTTP| Status
    Client -->|HTTP| Ingest
    Client -->|HTTP| Audit
    Client -->|HTTP| Summary
    Client -->|HTTP| Export
    Client -->|HTTP| Clear

    Ingest -->|embed chunks| Ollama
    Ingest -->|upsert vectors| Chroma
    Audit -->|query evidence| Chroma
    Audit -->|evaluate controls| Ollama
    Summary -->|generate text| Ollama
    Export -->|compile PDF| Pdflatex
    Clear -->|wipe collection| Chroma
```

## Tech Stack

- **Backend**: Python, FastAPI, PyMuPDF (PDF parsing), ChromaDB (vector storage)
- **AI/LLM**: Ollama (`llama3.1:8b` for reasoning, `nomic-embed-text` for embeddings)
- **Frontend**: React 19, TypeScript, Vite, Tailwind CSS
- **Document Generation**: TeX Live (`pdflatex`)

## System Requirements

- **Python**: 3.11+
- **Node.js**: 20+
- **Ollama**: 0.1.40+
- **TeX Live**: 2024+
- **Storage**: ~10 GB for AI models

## Installation Guide

### 1. Download Local AI Models

Ensure Ollama is running, then pull the required models:

```bash
ollama serve &
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

### 2. Install TeX Live

Required for generating PDF reports locally.

- **Arch Linux (using pacman or yay)**:
  ```bash
  sudo pacman -S texlive-basic texlive-latexextra texlive-fontsrecommended
  # or via yay:
  yay -S texlive-basic texlive-latexextra texlive-fontsrecommended
  ```
- **Fedora**:
  ```bash
  sudo dnf install -y texlive-scheme-basic texlive-collection-latexextra texlive-collection-fontsrecommended
  ```
- **Debian / Ubuntu**:
  ```bash
  sudo apt install texlive-latex-extra texlive-fonts-recommended
  ```
- **macOS**:
  ```bash
  brew install --cask mactex-no-gui
  ```

### 3. Setup Backend

Initialize the Python environment and install dependencies:

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install fastapi uvicorn httpx pydantic chromadb pymupdf
```

### 4. Setup Frontend

Install the React dependencies:

```bash
cd frontend
npm install
```

## Running the Application

Launch the required services in three separate terminal windows:

1. **Ollama Service**:
   ```bash
   ollama serve
   ```

2. **Backend API**:
   ```bash
   cd backend
   source venv/bin/activate
   python main.py
   # Runs at http://localhost:8000 (Swagger docs available at /docs)
   ```

3. **Frontend Application**:
   ```bash
   cd frontend
   npm run dev
   # Runs at http://localhost:5173
   ```

## How the Pipeline Operates

1. **Ingestion**: Uploaded documents are parsed into text chunks, embedded via the `nomic-embed-text` model, and stored locally in a ChromaDB database.
2. **Evaluation**: When auditing against a standard, the system searches the vector database for relevant evidence. The `llama3.1` model then analyzes the retrieved content to determine a compliance status and formulate recommendations.
3. **Scoring**: The overall compliance posture is quantified using a weighted metric:

   $$
   \text{Trust Score} = \frac{\sum_i w_i \cdot s_i}{\sum_i w_i} \times 100
   $$

   *(where $w_i$ represents the weight of a given control and $s_i$ represents the compliance score achieved for that control).*
4. **Executive Summary**: The LLM crafts a professional, translated summary of the audit findings in your chosen language.
5. **PDF Export**: The system dynamically generates a LaTeX document containing the detailed findings, scores, and summary. It compiles this document locally into a color-coded, ready-to-share PDF report.

## Frontend Overview

The web interface is designed for simplicity and efficiency:
- **Document & Standard Selection**: Easily upload PDFs and choose from built-in or custom compliance frameworks.
- **Audit Dashboard**: View high-level metrics, an interactive compliance heatmap, and an ordered list of findings based on severity.
- **Report Management**: Generate AI summaries, preview results, and export professional PDF deliverables directly from the browser. Local storage retains previous audits and uploaded documents for quick access.

## Supported Frameworks

The system includes pre-configured controls for:
- ISO 27001 (Information Security)
- ISO 9001 (Quality Management)
- ISO 14001 (Environmental Management)
- ISO 45001 (Occupational Health & Safety)
- GDPR (Data Protection)
- SOC 2 (Service Organization Controls)

Custom standards can also be defined and imported directly via the frontend dashboard.

## Configuration Options

Default settings can be adjusted in the backend configuration files (`main.py` and `evaluator.py`):
- API timeouts and Ollama URL endpoints
- Specific LLM model choices
- PDF file size limits and compilation timeouts