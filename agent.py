import sys
import json
import uuid
import subprocess
import traceback
import logging
import time
import ast  # CHANGED: for syntax validation without execution
from pathlib import Path
from typing import Annotated
from datetime import datetime

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool, BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict
from dotenv import load_dotenv
load_dotenv()

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agent.log", encoding="utf-8"),
    ],
)

logger = logging.getLogger("agent")

log_sandbox = logging.getLogger("agent.sandbox")
log_registry = logging.getLogger("agent.registry")
log_tools = logging.getLogger("agent.tools")
log_graph = logging.getLogger("agent.graph")
log_runner = logging.getLogger("agent.runner")

logger.info("Logging initialised — level=DEBUG, output=stdout + agent.log")

SANDBOX_DIR = Path(__file__).parent / "sandbox_env"
SANDBOX_DIR.mkdir(exist_ok=True)
logger.debug("SANDBOX_DIR: %s", SANDBOX_DIR)

TOOLS_DIR = SANDBOX_DIR / "tools"
TOOLS_DIR.mkdir(exist_ok=True)
logger.debug("TOOLS_DIR: %s", TOOLS_DIR)

OUTPUTS_DIR = SANDBOX_DIR / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)
logger.debug("OUTPUTS_DIR: %s", OUTPUTS_DIR)

VENV_DIR = SANDBOX_DIR / "venv"
logger.debug("VENV_DIR: %s", VENV_DIR)


def ensure_venv():
    if not VENV_DIR.exists():
        log_sandbox.info("Virtual-env not found — creating at %s", VENV_DIR)
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
            capture_output=True,
        )
        log_sandbox.info("Virtual-env created in %.2fs", time.perf_counter() - t0)
    else:
        log_sandbox.debug("Virtual-env already exists at %s", VENV_DIR)
    return VENV_DIR


def get_venv_python():
    ensure_venv()
    if sys.platform == "win32":
        python = str(VENV_DIR / "Scripts" / "python.exe")
    else:
        python = str(VENV_DIR / "bin" / "python")
    log_sandbox.debug("Resolved sandbox Python: %s", python)
    return python


def install_package_in_sandbox(package_name: str) -> dict:
    log_sandbox.info("Installing package '%s' in sandbox …", package_name)
    python = get_venv_python()
    t0 = time.perf_counter()
    result = subprocess.run(
        [python, "-m", "pip", "install", package_name, "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.perf_counter() - t0
    outcome = {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "package": package_name,
    }
    if outcome["success"]:
        log_sandbox.info(
            "Package '%s' installed successfully (%.2fs)", package_name, elapsed
        )
    else:
        log_sandbox.error(
            "Failed to install '%s' (%.2fs) — stderr: %s",
            package_name, elapsed, result.stderr.strip(),
        )
    return outcome


def execute_code_in_sandbox(code: str, timeout: int = 60) -> dict:
    code_preview = code[:120].replace("\n", "\\n")
    log_sandbox.info(
        "Executing code in sandbox (timeout=%ds) — preview: %s…", timeout, code_preview
    )
    python = get_venv_python()

    code_file = SANDBOX_DIR / f"exec_{uuid.uuid4().hex[:8]}.py"
    code_file.write_text(code, encoding="utf-8")
    log_sandbox.debug("Temp code file written: %s (%d bytes)", code_file, len(code))

    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            [python, str(code_file)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(OUTPUTS_DIR),
        )
        elapsed = time.perf_counter() - t0
        success = result.returncode == 0
        if success:
            log_sandbox.info("Code executed successfully in %.2fs", elapsed)
            if result.stdout:
                log_sandbox.debug("stdout: %s", result.stdout[:500])
        else:
            log_sandbox.error(
                "Code execution failed (rc=%d) in %.2fs — stderr: %s",
                result.returncode, elapsed, result.stderr[:500],
            )
        return {
            "success": success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "code": code,
        }
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        log_sandbox.error(
            "Code execution timed out after %.2fs (limit=%ds)", elapsed, timeout
        )
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Execution timed out after {timeout}s",
            "code": code,
        }
    finally:
        code_file.unlink(missing_ok=True)
        log_sandbox.debug("Temp code file removed: %s", code_file)


class DynamicToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._tool_source: dict[str, str] = {}
        log_registry.debug("DynamicToolRegistry initialised (empty)")

    def register(self, name: str, description: str, func):
        log_registry.info("Registering tool '%s'", name)
        t = StructuredTool.from_function(
            func=func,
            name=name,
            description=description,
        )
        self._tools[name] = t
        log_registry.debug("Tool '%s' registered successfully", name)
        return t

    def get_all(self) -> list[BaseTool]:
        names = list(self._tools.keys())
        log_registry.debug("get_all() → %d tool(s): %s", len(names), names)
        return list(self._tools.values())

    def get(self, name: str) -> BaseTool | None:
        tool = self._tools.get(name)
        if tool is None:
            log_registry.debug("get('%s') → not found", name)
        else:
            log_registry.debug("get('%s') → found", name)
        return tool

    def list_names(self) -> list[str]:
        names = list(self._tools.keys())
        log_registry.debug("list_names() → %s", names)
        return names

    def save_source(self, name: str, source: str):
        self._tool_source[name] = source
        path = TOOLS_DIR / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        log_registry.info(
            "Tool source for '%s' saved to %s (%d bytes)", name, path, len(source)
        )


tool_registry = DynamicToolRegistry()


@tool
def install_package(package_name: str) -> str:
    """
    Install a Python package into the sandboxed environment.
    Use this before writing tools that depend on third-party libraries.
    package_name: e.g. 'reportlab', 'matplotlib', 'requests'
    """
    log_tools.info("[install_package] called with package_name='%s'", package_name)
    result = install_package_in_sandbox(package_name)
    if result["success"]:
        msg = f"Successfully installed '{package_name}' in sandbox."
        log_tools.info("[install_package] %s", msg)
    else:
        msg = f"Failed to install '{package_name}':\n{result['stderr']}"
        log_tools.error("[install_package] %s", msg)
    return msg


# CHANGED: New helper to validate Python syntax without executing in main process
def validate_python_syntax(source_code: str, tool_name: str) -> tuple[bool, str]:
    """Validate that source_code is valid Python syntax using ast.parse."""
    try:
        ast.parse(source_code)
        return True, ""
    except SyntaxError as e:
        return False, f"Syntax error in tool '{tool_name}': {e}"


# CHANGED: New helper to execute a dynamic tool in the sandbox venv via subprocess
def execute_dynamic_tool_in_sandbox(tool_name: str, inputs: dict, timeout: int = 60) -> dict:
    """
    Execute a dynamic tool by running its source file in the sandbox venv.
    The tool's run() function is called with the inputs dict.
    """
    python = get_venv_python()

    # Build a wrapper script that imports the tool and calls run()
    wrapper_code = f'''
import sys
import json

# Add tools dir to path so the module can be found
sys.path.insert(0, r"{TOOLS_DIR}")

from {tool_name} import run

inputs = {json.dumps(inputs)}
result = run(inputs)
print(json.dumps(result))
'''

    wrapper_file = SANDBOX_DIR / f"wrapper_{tool_name}_{uuid.uuid4().hex[:8]}.py"
    wrapper_file.write_text(wrapper_code, encoding="utf-8")

    try:
        result = subprocess.run(
            [python, str(wrapper_file)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(OUTPUTS_DIR),
        )

        if result.returncode != 0:
            return {
                "message": f"Tool execution failed: {result.stderr}",
                "traceback": result.stderr,
            }

        # Parse the JSON output from the tool
        output = result.stdout.strip()
        if output:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return {"message": output}
        else:
            return {"message": "Tool executed but produced no output"}

    except subprocess.TimeoutExpired:
        return {"message": f"Tool execution timed out after {timeout}s"}
    except Exception as e:
        return {"message": f"Tool execution error: {e}", "traceback": traceback.format_exc()}
    finally:
        wrapper_file.unlink(missing_ok=True)


@tool
def write_and_register_tool(
    tool_name: str,
    tool_description: str,
    tool_code: str,
) -> str:
    """
    Write Python code for a new tool, save it, and register it so the agent
    can call it in subsequent steps.

    tool_name: snake_case name for the new tool (e.g. 'generate_pdf')
    tool_description: what this tool does (used by the LLM to decide when to call it)
    tool_code: Complete Python code. Must define a function named EXACTLY `run(inputs: dict) -> dict`.
    """
    log_tools.info(
        "[write_and_register_tool] called — tool_name='%s', description_len=%d, code_len=%d",
        tool_name, len(tool_description), len(tool_code),
    )

    # CHANGED: Validate syntax without executing in main process
    is_valid, error_msg = validate_python_syntax(tool_code, tool_name)
    if not is_valid:
        log_tools.error("[write_and_register_tool] %s", error_msg)
        return error_msg

    # CHANGED: Check that the code defines a run function
    try:
        tree = ast.parse(tool_code)
        has_run = any(
            isinstance(node, ast.FunctionDef) and node.name == "run"
            for node in ast.walk(tree)
        )
        if not has_run:
            log_tools.error(
                "[write_and_register_tool] module '%s' has no `run` function", tool_name
            )
            return "Tool code must define a `run(inputs: dict) -> dict` function."
    except Exception as e:
        return f"Failed to parse tool code: {e}"

    tool_registry.save_source(tool_name, tool_code)

    mod_path = TOOLS_DIR / f"{tool_name}.py"
    mod_path.write_text(tool_code, encoding="utf-8")
    log_tools.debug(
        "[write_and_register_tool] source written to %s (%d bytes)", mod_path, len(tool_code)
    )

    # CHANGED: Instead of exec_module in main process, we create a wrapper that runs in sandbox
    def tool_func(inputs: str) -> str:
        log_tools.info(
            "[%s] invoked — raw inputs type=%s, preview=%s",
            tool_name, type(inputs).__name__, str(inputs)[:200],
        )
        try:
            if isinstance(inputs, str):
                parsed = json.loads(inputs)
                log_tools.debug("[%s] parsed JSON inputs: %s", tool_name, parsed)
            else:
                parsed = inputs
        except Exception as parse_err:
            log_tools.warning(
                "[%s] could not parse inputs as JSON (%s) — falling back to raw dict",
                tool_name, parse_err,
            )
            parsed = {"raw": inputs}

        t0 = time.perf_counter()
        try:
            # CHANGED: Execute in sandbox venv instead of main process
            result = execute_dynamic_tool_in_sandbox(tool_name, parsed)
            elapsed = time.perf_counter() - t0
            log_tools.info(
                "[%s] run() completed in %.2fs — result keys: %s",
                tool_name, elapsed, list(result.keys()) if isinstance(result, dict) else "N/A",
            )
            if isinstance(result, dict) and result.get("output_file"):
                log_tools.info(
                    "[%s] output_file reported: %s", tool_name, result["output_file"]
                )
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log_tools.error(
                "[%s] run() raised an exception after %.2fs: %s\n%s",
                tool_name, elapsed, e, traceback.format_exc(),
            )
            result = {"message": f"Error: {e}", "traceback": traceback.format_exc()}

        serialised = json.dumps(result)
        log_tools.debug("[%s] serialised result length: %d bytes", tool_name, len(serialised))
        return serialised

    st = StructuredTool.from_function(
        func=tool_func,
        name=tool_name,
        description=tool_description,
    )
    tool_registry._tools[tool_name] = st

    success_msg = (
        f"Tool '{tool_name}' has been written, registered, and is now available.\n"
        f"Description: {tool_description}\n"
        f"Source saved to: {mod_path}"
    )
    log_tools.info(
        "[write_and_register_tool] tool '%s' registered and ready", tool_name
    )
    return success_msg


@tool
def execute_python_code(code: str) -> str:
    """
    Execute arbitrary Python code in the sandboxed environment.
    Use this for quick computations or testing before writing a full tool.
    Returns stdout/stderr from execution.
    """
    log_tools.info(
        "[execute_python_code] called — code_len=%d, preview=%s…",
        len(code), code[:80].replace("\n", "\\n"),
    )
    result = execute_code_in_sandbox(code)
    if result["success"]:
        output = f"Execution succeeded:\n{result['stdout']}"
        log_tools.info("[execute_python_code] execution succeeded")
        log_tools.debug("[execute_python_code] stdout: %s", result["stdout"][:300])
    else:
        output = f"Execution failed:\n{result['stderr']}\n{result['stdout']}"
        log_tools.error(
            "[execute_python_code] execution failed — stderr: %s", result["stderr"][:300]
        )
    return output


@tool
def list_available_tools() -> str:
    """List all dynamically created tools available to the agent."""
    log_tools.info("[list_available_tools] called")
    names = tool_registry.list_names()
    if not names:
        log_tools.info("[list_available_tools] no dynamic tools registered yet")
        return "No dynamic tools created yet."
    log_tools.info("[list_available_tools] %d tool(s): %s", len(names), names)
    return "Available dynamic tools:\n" + "\n".join(f"  - {name}" for name in names)


@tool
def read_output_file(filename: str) -> str:
    """
    Read metadata about a file from the sandbox outputs directory.
    Returns a lightweight reference — NEVER base64.
    The UI fetches binary content separately via HTTP.
    """
    log_tools.info("[read_output_file] called — filename='%s'", filename)

    path = OUTPUTS_DIR / filename
    if not path.exists():
        log_tools.debug(
            "[read_output_file] not in OUTPUTS_DIR — trying as absolute path: %s", filename
        )
        path = Path(filename)
        if not path.exists():
            log_tools.warning("[read_output_file] file not found: %s", filename)
            return f"File not found: {filename}"

    suffix = path.suffix.lower()
    binary_types = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".xlsx", ".zip"}
    is_binary = suffix in binary_types

    stat = path.stat()
    log_tools.info(
        "[read_output_file] found '%s' — size=%d bytes, is_binary=%s",
        path.name, stat.st_size, is_binary,
    )

    metadata = {
        "file_exists": True,
        "filename": path.name,
        "extension": suffix.lstrip("."),
        "is_binary": is_binary,
        "size_bytes": stat.st_size,
        "absolute_path": str(path.absolute()),
        "ui_endpoint": f"/artifacts/{path.name}",
        "note": "File is available. Do NOT attempt to read binary content as text."
    }
    log_tools.debug("[read_output_file] metadata: %s", metadata)
    return json.dumps(metadata)


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    ui_messages: list[dict]
    artifacts: list[dict]
    tool_history: list[str]
    iteration: int


SYSTEM_PROMPT = """You are a Self-Evolving AI Agent. You start with zero domain-specific tools and build them on the fly.

## Your Core Capabilities
1. **install_package** - Install any Python package in your sandboxed environment
2. **write_and_register_tool** - Write Python code for a new tool and register it for immediate use
3. **execute_python_code** - Run quick Python snippets
4. **list_available_tools** - See what tools you've created
5. **read_output_file** - Read generated files (returns base64 for PDFs/images)

## How You Work
When given a task:
1. **Analyze** what the task needs (PDF generation? Charts? Data fetching?)
2. **Install** any required packages first
3. **Write a tool** using `write_and_register_tool` - the tool must define `run(inputs: dict) -> dict`
4. **Call the tool** with appropriate inputs as a JSON string
5. **Read the output** using `read_output_file` if a file was generated
6. **Present** the result clearly, including base64 data for files

## Writing Tools
Tools must follow this pattern:
```python
def run(inputs: dict) -> dict:
    # Your implementation here
    # Save output files to: /absolute/path/in/sandbox/outputs/
    return {
        "message": "Human-readable description of what was done",
        "output_file": "/full/absolute/path/to/output.pdf",  # if a file was created
        "data": {}  # any additional structured data
    }
```

## Important Rules
- Always install packages BEFORE writing tools that use them
- Output files must be saved with ABSOLUTE paths to the outputs directory
- When a tool generates a file, always call `read_output_file` to get its content
- Be proactive: if a task requires multiple steps, plan and execute them all
- You evolve with each conversation - tools you create persist for this session

The outputs directory is: """ + str(OUTPUTS_DIR) + """

Be thorough, creative, and autonomous. You have full freedom to solve problems by writing whatever code you need."""


def build_agent():
    """Build the LangGraph self-evolving agent."""
    log_graph.info("build_agent() — constructing LangGraph agent")

    llm = ChatOpenAI(model="gpt-4o")  # CHANGED: Fixed model name from invalid "gpt-5.4"
    log_graph.debug("LLM initialised: %s", llm.model_name if hasattr(llm, "model_name") else llm)

    def get_all_tools():
        builtin = [
            install_package,
            write_and_register_tool,
            execute_python_code,
            list_available_tools,
            read_output_file,
        ]
        dynamic = tool_registry.get_all()
        all_tools = builtin + dynamic
        log_graph.debug(
            "get_all_tools() → %d built-in + %d dynamic = %d total",
            len(builtin), len(dynamic), len(all_tools),
        )
        return all_tools

    def agent_node(state: AgentState):
        iteration = state.get("iteration", 0) + 1
        log_graph.info("---- agent_node - iteration #%d ----", iteration)

        all_tools = get_all_tools()
        llm_with_tools = llm.bind_tools(all_tools)
        log_graph.debug("LLM bound with %d tool(s)", len(all_tools))

        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        log_graph.debug(
            "Sending %d message(s) to LLM (incl. system prompt)", len(messages)
        )

        t0 = time.perf_counter()
        response = llm_with_tools.invoke(messages)
        elapsed = time.perf_counter() - t0

        tool_calls = getattr(response, "tool_calls", [])
        log_graph.info(
            "LLM responded in %.2fs — tool_calls=%d, content_len=%s",
            elapsed,
            len(tool_calls),
            len(str(response.content)) if response.content else 0,
        )
        if tool_calls:
            for tc in tool_calls:
                log_graph.info(
                    "  ↳ tool_call: name='%s', args_keys=%s",
                    tc["name"], list(tc["args"].keys()) if isinstance(tc["args"], dict) else tc["args"],
                )

        return {
            "messages": [response],
            "iteration": iteration,
        }

    def truncate_artifact_content(content: str, max_preview: int = 500) -> str:
        """Replace base64 blobs with lightweight placeholders to save tokens."""
        if not isinstance(content, str):
            log_graph.debug("truncate_artifact_content: non-string input (%s)", type(content))
            return content

        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "base64" in parsed:
                safe = {k: v for k, v in parsed.items() if k != "base64"}
                safe["content_truncated"] = True
                safe["note"] = "Base64 content removed to save tokens. File is available in artifacts."
                log_graph.debug(
                    "truncate_artifact_content: stripped base64 blob (original=%d bytes)",
                    len(content),
                )
                return json.dumps(safe)
        except (json.JSONDecodeError, TypeError):
            pass

        if len(content) > 4000:
            log_graph.debug(
                "truncate_artifact_content: content too long (%d chars) — truncating to %d",
                len(content), max_preview,
            )
            return content[:max_preview] + f"\n... [truncated, total length: {len(content)} chars]"

        return content

    # CHANGED: New helper to extract artifact info from tool results
    def extract_artifact_from_result(full_content: str) -> dict | None:
        """Extract artifact metadata from a tool result that contains an output_file."""
        if not isinstance(full_content, str):
            return None

        try:
            parsed = json.loads(full_content)
            if isinstance(parsed, dict):
                # Check for output_file
                output_file = parsed.get("output_file")
                if output_file:
                    path = Path(output_file)
                    if path.exists():
                        suffix = path.suffix.lower()
                        return {
                            "filename": path.name,
                            "extension": suffix.lstrip("."),
                            "size_bytes": path.stat().st_size,
                            "timestamp": datetime.now().isoformat(),
                        }
                # Also check old-style base64
                if "base64" in parsed:
                    return {
                        "filename": parsed.get("filename", "output"),
                        "extension": parsed.get("extension", "bin"),
                        "size_bytes": parsed.get("size_bytes", 0),
                        "timestamp": datetime.now().isoformat(),
                    }
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def tool_executor_node(state: AgentState):
        """Execute tool calls, including dynamically registered ones."""
        last_message = state["messages"][-1]
        tool_calls = getattr(last_message, "tool_calls", [])
        log_graph.info(
            "---- tool_executor_node - %d tool call(s) to execute ----", len(tool_calls)
        )

        all_tools = get_all_tools()
        tool_map = {t.name: t for t in all_tools}
        log_graph.debug("Tool map keys: %s", list(tool_map.keys()))

        results = []
        artifacts = list(state.get("artifacts", []))
        tool_history = list(state.get("tool_history", []))

        for idx, tc in enumerate(tool_calls, start=1):
            tool_name = tc["name"]
            tool_args = tc["args"]
            tool_call_id = tc["id"]
            log_graph.info(
                "  [%d/%d] Executing tool '%s' (call_id=%s)",
                idx, len(tool_calls), tool_name, tool_call_id,
            )
            log_graph.debug("  args: %s", str(tool_args)[:300])

            t = tool_map.get(tool_name)
            if t is None:
                log_graph.debug(
                    "  '%s' not in tool_map — checking dynamic registry", tool_name
                )
                t = tool_registry.get(tool_name)
            if t is None:
                full_content = f"Tool '{tool_name}' not found."
                log_graph.error("  Tool '%s' not found in any registry", tool_name)
            else:
                t0 = time.perf_counter()
                try:
                    full_content = t.invoke(tool_args)
                    elapsed = time.perf_counter() - t0
                    log_graph.info(
                        "  Tool '%s' returned in %.2fs (output_len=%d)",
                        tool_name, elapsed, len(str(full_content)),
                    )
                except Exception as e:
                    elapsed = time.perf_counter() - t0
                    full_content = f"Tool execution error: {e}\n{traceback.format_exc()}"
                    log_graph.error(
                        "  Tool '%s' raised an exception after %.2fs: %s",
                        tool_name, elapsed, e,
                    )

            # CHANGED: Extract artifacts from both base64 AND output_file patterns
            artifact_entry = extract_artifact_from_result(full_content)
            if artifact_entry:
                # Avoid duplicates
                if not any(a.get("filename") == artifact_entry["filename"] for a in artifacts):
                    artifacts.append(artifact_entry)
                    log_graph.info(
                        "  Artifact registered: %s (%d bytes)",
                        artifact_entry["filename"], artifact_entry["size_bytes"],
                    )

            llm_content = truncate_artifact_content(full_content)

            if tool_name == "write_and_register_tool":
                created_name = tool_args.get("tool_name", "unknown")
                tool_history.append(created_name)
                log_graph.info(
                    "  Recorded new dynamic tool in history: '%s'", created_name
                )

            results.append(
                ToolMessage(
                    content=llm_content,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
            )

        log_graph.info(
            "tool_executor_node complete — %d result(s), %d artifact(s) total, tool_history=%s",
            len(results), len(artifacts), tool_history,
        )
        return {
            "messages": results,
            "artifacts": artifacts,
            "tool_history": tool_history,
        }

    def should_continue(state: AgentState) -> str:
        """Route: continue tool use or end."""
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            log_graph.debug(
                "should_continue → 'tools' (%d pending call(s))", len(last.tool_calls)
            )
            return "tools"
        log_graph.debug("should_continue → END (no pending tool calls)")
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_executor_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    compiled = graph.compile()
    log_graph.info("LangGraph agent compiled successfully")
    return compiled


_agent = None


def get_agent():
    global _agent
    if _agent is None:
        log_runner.info("Agent not yet built — calling build_agent()")
        _agent = build_agent()
        log_runner.info("Agent built and cached")
    else:
        log_runner.debug("Returning cached agent instance")
    return _agent


def run_agent(user_message: str, history: list[dict]) -> dict:
    log_runner.info(
        "run_agent() called — message_len=%d, history_turns=%d",
        len(user_message), len(history),
    )
    log_runner.debug("User message: %s", user_message[:300])

    agent = get_agent()

    # Convert chat history to LangChain messages
    lc_history: list[BaseMessage] = []
    for i, msg in enumerate(history):
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            lc_history.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_history.append(AIMessage(content=content))
        else:
            log_runner.warning("Unknown history message role '%s' at index %d — skipping", role, i)

    lc_history.append(HumanMessage(content=user_message))
    log_runner.debug(
        "LangChain history prepared: %d message(s) (incl. new user msg)", len(lc_history)
    )

    initial_state: AgentState = {
        "messages": lc_history,
        "artifacts": [],
        "tool_history": [],
        "iteration": 0,
    }

    log_runner.info("Invoking LangGraph agent (recursion_limit=50) …")
    t0 = time.perf_counter()
    result = agent.invoke(initial_state, config={"recursion_limit": 50})
    elapsed = time.perf_counter() - t0
    log_runner.info(
        "Agent invocation complete in %.2fs — total_messages=%d, artifacts=%d, tool_history=%s",
        elapsed,
        len(result["messages"]),
        len(result.get("artifacts", [])),
        result.get("tool_history", []),
    )

    final_msg = result["messages"][-1]
    response_text = ""
    if hasattr(final_msg, "content"):
        if isinstance(final_msg.content, list):
            for block in final_msg.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    response_text += block["text"]
                elif isinstance(block, str):
                    response_text += block
        else:
            response_text = str(final_msg.content)

    log_runner.info(
        "Final response assembled — length=%d chars", len(response_text)
    )
    log_runner.debug("Response preview: %s…", response_text[:200])

    steps = []
    for msg in result["messages"]:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                step = {
                    "type": "tool_call",
                    "tool": tc["name"],
                    "args": tc["args"],
                }
                steps.append(step)
                log_runner.debug("Step recorded: tool_call '%s'", tc["name"])
        if isinstance(msg, ToolMessage):
            content_preview = str(msg.content)[:300]
            step = {
                "type": "tool_result",
                "tool": msg.name,
                "preview": content_preview,
            }
            steps.append(step)
            log_runner.debug("Step recorded: tool_result for '%s'", msg.name)

    log_runner.info("run_agent() returning — steps=%d", len(steps))

    return {
        "response": response_text,
        "artifacts": result.get("artifacts", []),
        "tool_history": result.get("tool_history", []),
        "steps": steps,
    }
