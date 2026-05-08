# Self-Evolving AI

## What It Is

This is a **self-evolving autonomous AI agent** built with Python and LangGraph. Unlike traditional agents that come with a fixed toolkit, this agent starts with **zero domain-specific tools**. When given a task, it analyzes what capabilities it needs, writes its own Python tools at runtime, installs any required third-party packages into an isolated sandbox, executes the tools, and delivers the results, all without human intervention.

---

## The Core Problem It Solves

Most AI agents have fixed no of tool, if you give a task that requires extra knowledge or usage of pre configured tools, the agent simply doesnt have the capability to perform that task.. since it lacsk that required tools. So this Project allows the ai to **write the tool it wants during user conversation and then execute it on the fly**
---

## Architecture Overview

The system has three conceptual layers:

1. **The Brain**: A Large Language Model (OpenAI GPT-5.4) that reasons about tasks and decides what tools to build
2. **The Workshop**: A dynamic tool registry where the agent writes, saves, and loads Python modules
3. **The Sandbox**: An isolated virtual environment where all untrusted code runs safely

---

## Key Components Explained

### 1. The Sandbox Environment (`SANDBOX_DIR`, `VENV_DIR`, `TOOLS_DIR`, `OUTPUTS_DIR`)

The agent never executes user-influenced or self-generated code in the main Python process. Instead, it maintains a **completely isolated virtual environment** inside a `sandbox_env` folder.

- **`venv/`**: A standalone Python virtual environment created with `python -m venv`. All third-party packages (like `reportlab`, `matplotlib`) are installed here, keeping the host environment clean.
- **`tools/`**: A module directory where dynamically written tools are saved as `.py` files. These persist across the session, so the agent "remembers" tools it built earlier.
- **`outputs/`**: The designated directory where generated files (PDFs, images, CSVs) are saved. The agent instructs its tools to write here, and the UI fetches results from this location.

---

### 2. The Dynamic Tool Registry (`DynamicToolRegistry`)

This is the agent's **memory of what it has built**. It is a Python class that maintains two dictionaries:

- **`_tools`**: Maps tool names to LangChain `StructuredTool` objects. These are the actual callable tools the LLM can invoke.
- **`_tool_source`**: Maps tool names to the original source code strings. This allows inspection, debugging, and persistence.

**Registration flow:** When the agent writes a tool, the registry saves the `.py` file to disk, validates the syntax using Python's `ast` module (without executing it), and creates a wrapper function that the LLM can call. The tool is immediately available for use in subsequent conversation turns.

---

### 3. The Five Built-in Meta-Tools

The agent starts with only five fundamental tools. Everything else is built on top of these:

| Tool | Purpose |
|------|---------|
| **`install_package`** | Installs any PyPI package into the sandbox venv. The agent calls this before writing tools that depend on external libraries. |
| **`write_and_register_tool`** | The most powerful tool. Accepts a tool name, description, and complete Python source code. Validates syntax, saves to disk, and registers it for immediate use. |
| **`execute_python_code`** | Runs arbitrary Python snippets in the sandbox for quick testing or one-off computations without writing a full tool. |
| **`list_available_tools`** | Returns the list of dynamically created tools so the agent can check what it already knows. |
| **`read_output_file`** | Reads metadata about generated files from the outputs directory. Returns lightweight metadata (filename, size, type). |

---

### 4. How Dynamic Tools Actually Run (The Critical Innovation)

Here is the most important architectural detail: **Dynamic tools do NOT run in the main Python process.**

When the agent writes a tool like `generate_solar_system_pdf`, that tool imports `reportlab`. But `reportlab` was installed in the sandbox venv, not the main environment. If we used standard `importlib` to load the module into the main process, it would fail with `ModuleNotFoundError`.

**The solution:** The `write_and_register_tool` function creates a wrapper that, when invoked, does the following:

1. Takes the input dictionary and serializes it to JSON
2. Generates a temporary "wrapper script" that imports the tool module and calls its `run()` function
3. Executes that wrapper script as a **subprocess** using the sandbox venv's Python interpreter
4. Captures the stdout, parses the JSON output, and returns it to the agent

This means every dynamic tool execution is:
- **Isolated**, Runs in the sandbox, not the main process **Safe**, A crash in the tool doesn't crash the agent
---


### 5. Token Cost Optimization

A major design concern was **preventing base64 data from entering the LLM context window**. A 100KB PDF encoded as base64 becomes ~75,000 tokens, which is expensive and wasteful.

The agent handles this through **content truncation**:

- When `read_output_file` is called, it returns only metadata: `{"filename": "x.pdf", "size_bytes": 5000, "extension": "pdf"}`
- The `tool_executor_node` intercepts large tool outputs and replaces base64 blobs with `{"content_truncated": true, "note": "File available in artifacts"}`
- The actual binary content is served to the UI via a separate **HTTP endpoint** (`/artifacts/base64/{filename}`), completely bypassing the LLM

---

### 6. Observability and Logging

The entire system has heavy structured logging:

- **Six named loggers** cover different subsystems: `agent.sandbox`, `agent.registry`, `agent.tools`, `agent.graph`, `agent.runner`
- Every significant operation is timed: venv creation, package installation, code execution, LLM calls
- Log output goes to both **console** (for real-time monitoring) and **file** (`agent.log` for post-hoc analysis)
- Key metrics captured: execution duration, output sizes, token counts (indirectly via content length), tool call sequences

---

## Example Execution Flow

Consider the user request: *"Create a PDF report about the solar system"*

1. **Iteration 1:** The LLM analyzes the request and decides it needs PDF generation. It calls `install_package("reportlab")`. The agent installs it into the sandbox venv.
2. **Iteration 2:** The LLM writes a complete Python tool `generate_solar_system_pdf` using `write_and_register_tool`. The source is saved to `sandbox_env/tools/`. Syntax is validated via AST. A subprocess wrapper is created.
3. **Iteration 3:** The LLM calls the newly registered `generate_solar_system_pdf` tool with `{"filename": "solar_system_report.pdf"}`. The wrapper executes the tool in the sandbox venv. The PDF is written to `sandbox_env/outputs/`.
4. **Iteration 4:** The LLM calls `read_output_file("solar_system_report.pdf")` to verify success. It receives metadata confirming the file exists (5,555 bytes).
5. **Iteration 5:** The LLM formulates a natural language response: *"I've generated a PDF report about the solar system..."* The graph routes to `END`.

The UI receives: the text response, the artifact metadata (for rendering), and the complete step-by-step execution trace.
