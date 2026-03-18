#!/usr/bin/env python3
"""
OpenClaw Bridge - SDK 调用层
负责调用 claude_agent_sdk 的 query() 函数，支持同步/异步模式
处理消息、异常捕获、安全规则检查
"""

import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

# 导入配置
import config


class BridgeError(Exception):
    """Bridge 专用异常"""
    pass


class SecurityError(BridgeError):
    """安全规则违规异常"""
    pass


class AskUserQuestionInterceptedError(BridgeError):
    """AskUserQuestion 拦截异常"""
    def __init__(self, question_data: Dict[str, Any]):
        self.question_data = question_data
        super().__init__(f"AskUserQuestion intercepted: {question_data.get('question', 'unknown')}")


class Bridge:
    """Bridge 主类"""

    def __init__(self, run_dir: str, skill_name: str, args: str = "",
                 model: str = None, backend: str = None, async_mode: bool = False):
        self.run_dir = Path(run_dir)
        self.skill_name = skill_name
        self.args = args
        self.backend = backend or config.DEFAULT_BACKEND
        # 修复: 使用 is not None 而非 or，避免空字符串被误替换
        self.model = model if model is not None else (
            config.DEFAULT_GEMINI_MODEL if self.backend == "gemini" else config.DEFAULT_MODEL
        )
        self.async_mode = async_mode

        self.state_file = self.run_dir / "state.json"
        self.output_file = self.run_dir / "output.txt"
        self.skill_dir = config.SKILLS_DIR / skill_name

        self.sdk = None
        self.process = None

    def _load_sdk(self):
        """加载 claude_agent_sdk"""
        try:
            import claude_agent_sdk
            return claude_agent_sdk
        except ImportError:
            raise BridgeError("claude_agent_sdk not installed. Run: pip install claude-agent-sdk")

    def _check_security(self, prompt: str) -> bool:
        """检查安全规则"""
        prompt_lower = prompt.lower()

        # 检查禁止的目录访问
        for forbidden_dir in config.FORBIDDEN_DIRS:
            if forbidden_dir.lower() in prompt_lower:
                # 特殊检查：如果是技能目录内的相对路径则允许
                if self.skill_dir.name not in prompt_lower:
                    raise SecurityError(f"Forbidden directory access detected: {forbidden_dir}")

        # 检查禁止的模式
        for pattern in config.FORBIDDEN_PATTERNS:
            pattern_name = pattern.replace("**/", "").replace("*", "")
            if pattern_name.lower() in prompt_lower:
                raise SecurityError(f"Forbidden pattern detected: {pattern}")

        return True

    def _check_required_files(self) -> bool:
        """检查必须先读取的文件"""
        for required_file in config.REQUIRED_FILES:
            required_path = self.skill_dir / required_file
            if not required_path.exists():
                raise SecurityError(f"Required file not found: {required_file}")

            # 验证文件不为空
            if required_path.stat().st_size == 0:
                raise SecurityError(f"Required file is empty: {required_file}")

        return True

    def _write_state(self, state: str, extra: Optional[Dict] = None):
        """原子写入状态（tmp + rename）"""
        state_data = {
            "state": state,
            "skill": self.skill_name,
            "args": self.args,
            "model": self.model,
            "run_dir": str(self.run_dir),
            "timestamp": datetime.now().isoformat(),
            "pid": os.getpid()
        }
        if extra:
            state_data.update(extra)

        # 写入临时文件
        tmp_file = self.state_file.with_suffix(config.TMP_STATE_SUFFIX)
        with open(tmp_file, "w") as f:
            json.dump(state_data, f, indent=2)

        # 原子重命名
        os.replace(tmp_file, self.state_file)

    def _read_state(self) -> Optional[Dict]:
        """读取状态"""
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def _output(self, message: str):
        """输出到 stdout 和文件"""
        print(message)
        with open(self.output_file, "a") as f:
            f.write(message + "\n")

    def _output_protocol(self, protocol: str, message: str = ""):
        """输出协议标记"""
        full_message = f"{protocol} {message}".strip()
        self._output(full_message)

    def _notify(self, status: str, message: str = ""):
        """通知回调"""
        notify_cmd = os.environ.get("OPENCLAW_NOTIFY_CMD", "")
        if not notify_cmd:
            return

        try:
            agent_id = os.environ.get("OPENCLAW_AGENT_ID", "unknown")
            message_encoded = message or status
            cmd = notify_cmd.format(agent_id=agent_id, message=message_encoded)
            os.system(cmd)
        except Exception as e:
            self._output(f"[BRIDGE:WARN] Notify failed: {e}")

    def _deliver_to_agent(self, result: Dict[str, Any]):
        """通过 openclaw agent --deliver 通知调用方"""
        agent_id = os.environ.get("OPENCLAW_AGENT_ID", "")
        if not agent_id:
            return

        try:
            # 构建消息
            status = result.get("state", "done")
            skill = result.get("skill", "unknown")
            output_preview = ""

            output_file = self.run_dir / "output.txt"
            if output_file.exists():
                with open(output_file) as f:
                    lines = f.readlines()
                    output_preview = "".join(lines[-20:])  # 最后 20 行

            message = json.dumps({
                "skill": skill,
                "status": status,
                "output": output_preview[:1000],  # 限制长度
                "run_dir": str(self.run_dir)
            })

            # 调用 deliver 命令 (使用正确的参数顺序)
            deliver_cmd = f"openclaw agent --deliver --agent-id {agent_id} --message '{message}'"
            os.system(deliver_cmd)
        except Exception as e:
            self._output(f"[BRIDGE:WARN] Deliver failed: {e}")

    def _build_prompt(self) -> str:
        """构建调用 Skill 的 prompt"""
        skill_md = self.skill_dir / "SKILL.md"
        prompt = f"# 调用 Skill: {self.skill_name}\n\n"

        if skill_md.exists():
            with open(skill_md) as f:
                prompt += f.read() + "\n\n"

        prompt += f"## 执行参数\n"
        prompt += f"- args: {self.args}\n"
        prompt += f"- async: {self.async_mode}\n"

        return prompt

    def _intercept_ask_user_question(self, tool_name: str, tool_input: Dict) -> bool:
        """拦截 AskUserQuestion 请求"""
        if tool_name != "AskUserQuestion":
            return False

        # 检查参数完整性
        questions = tool_input.get("questions", [])
        if not questions:
            return True  # 缺少问题，拦截

        # 检查每个问题是否有必要的字段
        for q in questions:
            if "question" not in q or "header" not in q or "options" not in q:
                # 参数不全，抛出拦截异常
                raise AskUserQuestionInterceptedError({
                    "tool": tool_name,
                    "reason": "incomplete_params",
                    "missing": "question/header/options",
                    "tool_input": tool_input
                })

        # 检查是否有 multiSelect 字段
        for q in questions:
            if "multiSelect" not in q:
                raise AskUserQuestionInterceptedError({
                    "tool": tool_name,
                    "reason": "incomplete_params",
                    "missing": "multiSelect",
                    "tool_input": tool_input
                })

        return False  # 参数完整，不拦截

    def _run_sync(self):
        """同步模式执行"""
        self._output_protocol(config.PROTOCOL_STARTING)
        self._write_state(config.State.STARTING)

        self._output_protocol(config.PROTOCOL_RUNNING)
        self._write_state(config.State.RUNNING, {"started_at": datetime.now().isoformat()})

        # 检查安全规则
        prompt = self._build_prompt()
        self._check_security(prompt)
        self._check_required_files()

        if self.backend == "gemini":
            self._run_gemini(prompt)
        else:
            self._run_claude(prompt)

    def _run_claude(self, prompt: str):
        """使用 Claude SDK 执行"""
        SDK = self._load_sdk()
        from claude_agent_sdk.types import ClaudeAgentOptions

        query_params = {
            "prompt": prompt,
            "options": ClaudeAgentOptions(
                model=self.model,
                max_thinking_tokens=config.SDK_MAX_TOKENS if hasattr(config, 'SDK_MAX_TOKENS') else None,
            )
        }

        if self.async_mode:
            query_params["stream"] = True

        try:
            result = SDK.query(**query_params)
            
            if self.async_mode:
                # 异步模式：处理流式输出
                for event in result:
                    if hasattr(event, "type"):
                        if event.type == "content_block_delta":
                            if hasattr(event, "delta") and hasattr(event.delta, "text"):
                                self._output(event.delta.text) # 保持输出一致性
                        elif event.type == "message_stop":
                            break
            else:
                # 同步模式：直接获取结果
                if hasattr(result, "content"):
                    output_text = ""
                    for block in result.content:
                        if hasattr(block, "text"):
                            output_text += block.text
                    self._output(output_text)

            self._write_state(config.State.DONE, {"completed_at": datetime.now().isoformat()})
            self._output_protocol(config.PROTOCOL_DONE, "Execution completed successfully")
            self._deliver_to_agent(self._read_state())
        except Exception as e:
            self._handle_error(e)

    def _run_gemini(self, prompt: str):
        """使用 Gemini CLI 执行"""
        import subprocess

        cmd = [
            config.GEMINI_PATH,
            "--prompt", prompt,
            "--model", self.model,
            "--approval-mode", config.GEMINI_APPROVAL_MODE,
            "--output-format", "stream-json"
        ]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                    event_type = event.get("type")

                    if event_type == "message":
                        role = event.get("role")
                        content = event.get("content", "")
                        # 处理 assistant 和 system 角色的消息
                        if content and role in ("assistant", "system"):
                            self._output(content)
                    
                    elif event_type == "tool_call":
                        tool_name = event.get("tool_name")
                        tool_input = event.get("tool_input", {})
                        
                        # 拦截 ask_user (Gemini 对应的工具名)
                        if tool_name == "ask_user":
                            raise AskUserQuestionInterceptedError({
                                "tool": "AskUserQuestion", # 映射到 OpenClaw 标准
                                "question": tool_input.get("question", ""),
                                "tool_input": tool_input
                            })

                except json.JSONDecodeError:
                    # 可能是非 JSON 输出（例如加载日志）
                    if not line.startswith("{"):
                        self._output(f"[GEMINI:LOG] {line}")

            return_code = self.process.wait()

            if return_code == 0:
                self._write_state(config.State.DONE, {"completed_at": datetime.now().isoformat()})
                self._output_protocol(config.PROTOCOL_DONE)
                self._deliver_to_agent(self._read_state())
            else:
                raise BridgeError(f"Gemini CLI exited with code {return_code}")

        except AskUserQuestionInterceptedError:
            # 重新抛出以由 run() 处理
            raise
        except Exception as e:
            self._handle_error(e)

    def _run_async(self):
        """异步模式（后台运行）"""
        import subprocess
        import signal

        # 启动子进程
        self.process = subprocess.Popen(
            [sys.executable, __file__,
             "--run-dir", str(self.run_dir),
             "--skill", self.skill_name,
             "--args", self.args,
             "--model", self.model,
             "--backend", self.backend],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        # 写入 PID
        self._write_state(config.State.RUNNING, {
            "child_pid": self.process.pid,
            "started_at": datetime.now().isoformat()
        })

        # 读取输出
        for line in self.process.stdout:
            print(line, end="")

        # 等待完成
        return_code = self.process.wait()

        if return_code == 0:
            self._write_state(config.State.DONE)
            self._output_protocol(config.PROTOCOL_DONE)
        else:
            self._write_state(config.State.ERROR, {"exit_code": return_code})
            self._output_protocol(config.PROTOCOL_ERROR, f"Exit code: {return_code}")
        
        # 确保异步模式结束时也进行通知
        self._deliver_to_agent(self._read_state())

    def _handle_error(self, error: Exception):
        """统一错误处理"""
        error_type = type(error).__name__
        error_msg = str(error)

        self._write_state(config.State.ERROR, {
            "error_type": error_type,
            "error_message": error_msg
        })

        self._output_protocol(config.PROTOCOL_ERROR, f"{error_type}: {error_msg}")

        # 通知
        state_data = self._read_state()
        self._deliver_to_agent(state_data)

    def run(self):
        """执行入口"""
        try:
            # 确保运行目录存在
            self.run_dir.mkdir(parents=True, exist_ok=True)

            # 检查 skill 目录
            if not self.skill_dir.exists():
                raise BridgeError(f"Skill not found: {self.skill_name}")

            # 根据模式执行
            if self.async_mode:
                self._run_async()
            else:
                self._run_sync()

        except AskUserQuestionInterceptedError as e:
            self._write_state(config.State.ERROR, {
                "error_type": "AskUserQuestionIntercepted",
                "question_data": e.question_data
            })
            self._output_protocol(config.PROTOCOL_ERROR, "AskUserQuestion intercepted - incomplete parameters")
            print(json.dumps({
                "intercepted": True,
                "question": e.question_data
            }), file=sys.stderr)

        except BaseException as e:
            self._handle_error(e)
            raise


def main():
    """主入口"""
    import argparse

    parser = argparse.ArgumentParser(description="OpenClaw Bridge")
    parser.add_argument("--run-dir", required=True, help="Run directory")
    parser.add_argument("--skill", required=True, help="Skill name")
    parser.add_argument("--args", default="", help="Skill arguments")
    parser.add_argument("--model", default=None, help="Model to use")
    parser.add_argument("--backend", default=None, choices=["claude", "gemini"], help="Backend to use")
    parser.add_argument("--async", dest="async_mode", action="store_true", help="Async mode")

    args = parser.parse_args()

    bridge = Bridge(
        run_dir=args.run_dir,
        skill_name=args.skill,
        args=args.args,
        model=args.model,
        backend=args.backend,
        async_mode=args.async_mode
    )

    bridge.run()


if __name__ == "__main__":
    main()
