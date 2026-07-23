#!/usr/bin/env python3
import os
import sys
import json
import platform
import subprocess
import ssl
import urllib.request
import urllib.error
import urllib.parse
import datetime
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Thread-safe multi-threaded HTTP server fallback
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    from socketserver import ThreadingMixIn
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        pass

# --- Configuration Section ---
CONFIG_DIR = os.path.expanduser("~/.config/nodemixaholic-software/openagent")
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
SYSTEM_PROMPT_FILE = os.path.join(CONFIG_DIR, "system_prompt.txt")
SKILLS_DIR = os.path.join(CONFIG_DIR, "skills")
CRON_FILE = os.path.join(CONFIG_DIR, "cron.json")

# Ensure config directory, skills directory, and .env file exist
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(SKILLS_DIR, exist_ok=True)

if not os.path.exists(ENV_FILE):
    with open(ENV_FILE, "w") as f:
        f.write("# OpenAgent configuration\n")

# Load environment variables manually to avoid dependencies
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"").replace("\r", "")
                os.environ[key] = val

# Set defaults
MODEL = os.environ.get("MODEL", "deepseek-v4-flash:cloud")
HOST = os.environ.get("HOST", "127.0.0.1:11434")
SEARCH_PREFIX = os.environ.get("SEARCH_PREFIX", "https://searx.nodemixaholic.com/search?q=")
VERIFY_SSL = os.environ.get("VERIFY_SSL", "false").lower() not in ("false", "0", "no")

PROJECT_DIR = os.getcwd()

# Thread-local storage for tracking agent outputs in API completions
current_thread_data = threading.local()

# Default System Prompt
DEFAULT_SYSTEM_PROMPT = """You are OpenAgent, an autonomous Unix-like systems agent.
Your goal is to solve the user's task step-by-step using the provided tools.

You have access to these tools:
1. read_file <file_path> : Reads a file's content. (to check a file's contents!)
2. append_file <file_path> <content> : Appends text to a file. (to append to a file!)
3. replace_file <file_path> <content> : Overwrites/creates a file with content. (to overwrite a file dangerously!)
4. list_files <dir_path> : Lists all files in a directory. (to list files in a directory!)
5. get_site_contents <url> : Gets contents of page via CURL (use this instead of pure curl!)
6. web_search <query> : Gets HTML source of a web search via CURL. (use this if web searching!)
7. agent_say <msg> : Echo out a message to the user. (to say something!)
8. bash <command> : Runs a standard shell command. ***(ONLY USE THIS WHEN ABSOLUTELY NEEDED. PRIORITIZE THE OTHER TOOLS.)***


Rules:
1. You work in an iterative loop. Output exactly ONE tool call at a time.
2. If the task is fully completed, output the word 'DONE' instead of a tool call.
3. Output ONLY the raw executable command or 'DONE'. Do not wrap in markdown, backticks, or write explanations.
4. The 'bash' tool is dangerous! ONLY USE 'bash' when ANOTHER TOOL or a combination of other tools CAN NOT do what you need."""

# Initialize or load the system prompt from the file
if not os.path.exists(SYSTEM_PROMPT_FILE):
    with open(SYSTEM_PROMPT_FILE, "w", encoding="utf-8") as f:
        f.write(DEFAULT_SYSTEM_PROMPT)

def get_system_prompt():
    if os.path.exists(SYSTEM_PROMPT_FILE):
        try:
            with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return DEFAULT_SYSTEM_PROMPT

# --- Helper Tool Functions ---
def read_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"

def append_file(filepath, content):
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(content + "\n")
        return f"Successfully appended to {filepath}"
    except Exception as e:
        return f"Error appending to file: {str(e)}"

def replace_file(filepath, content):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to {filepath}"
    except Exception as e:
        return f"Error writing to file: {str(e)}"

def run_bash(command):
    try:
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120
        )
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        if result.returncode != 0:
            return f"Error: Command failed with exit code {result.returncode}. Output:\n{output.strip()}"
        return output.strip()
    except Exception as e:
        return f"Error executing command: {str(e)}"

def list_files(dir):
    try:
        result = subprocess.run(
            "ls -lah " + dir,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120
        )
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        if result.returncode != 0:
            return f"Error: Listing directory failed with exit code {result.returncode}. Output:\n{output.strip()}"
        return output.strip()
    except Exception as e:
        return f"Error listing files: {str(e)}"

def agent_say(yap):
    try:
        print("<AI>: " + yap)
        if hasattr(current_thread_data, "yaps"):
            current_thread_data.yaps.append(yap)
        return "<AI>: " + yap
    except Exception as e:
        return f"Error yapping: {str(e)}"

def get_site_contents(url):
    try:
        result = subprocess.run(
            "curl -L '" + url + "'",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120
        )
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        if result.returncode != 0:
            return f"Error: Getting site contents failed with exit code {result.returncode}. Output:\n{output.strip()}"
        return output.strip()
    except Exception as e:
        return f"Error getting site contents: {str(e)}"

def web_search(q):
    try:
        url = SEARCH_PREFIX + urllib.parse.quote_plus(q)
        result = subprocess.run(
            "curl -L '" +  url + "'",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120
        )
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        if result.returncode != 0:
            return f"Error: Web search failed with exit code {result.returncode}. Output:\n{output.strip()}"
        return output.strip()
    except Exception as e:
        return f"Error getting web search contents: {e}"

# --- Tool Execution Router ---
def execute_tool(proposed_command):
    parts = proposed_command.strip().split(maxsplit=1)
    if not parts:
        return "Error: Empty command received."
    
    tool = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    if tool == "read_file":
        return read_file(args.strip())
    elif tool == "append_file":
        sub_parts = args.split(maxsplit=1)
        path = sub_parts[0] if len(sub_parts) > 0 else ""
        content = sub_parts[1] if len(sub_parts) > 1 else ""
        return append_file(path, content)
    elif tool == "replace_file":
        sub_parts = args.split(maxsplit=1)
        path = sub_parts[0] if len(sub_parts) > 0 else ""
        content = sub_parts[1] if len(sub_parts) > 1 else ""
        return replace_file(path, content)
    elif tool == "bash":
        return run_bash(args)
    elif tool == "list_files":
        return list_files(args if args else ".")
    elif tool == "agent_say":
        return agent_say(args)
    elif tool == "get_site_contents":
        return get_site_contents(args)
    elif tool == "web_search":
        return web_search(args)
    else:
        # Fallback: if no clear tool prefix is matched, treat the whole line as a bash command
        return run_bash(proposed_command)

# --- Functional Tool Verification ---
def is_tool_functional(cmd, output):
    output_lower = output.lower().strip()
    if output_lower.startswith("error:") or output_lower.startswith("error "):
        return False
    return True

# --- Main Agent Loop ---
def run_agent(prompt_request, collect_tools=False):
    os_name = platform.system()
    system_prompt = get_system_prompt()
    
    print(f"Starting task: {prompt_request}")
    print("--------------------------------------")

    history = f"User Request: {prompt_request}"
    step = 1
    max_steps = 200
    functioning_tools = []

    while step <= max_steps:
        print(f"Thinking (Step {step}, Model: {MODEL})...")

        full_prompt = (
            f"System: {system_prompt}\n"
            f"Context: OS: {os_name} | User: {os.environ.get('USER', 'unknown')} | PWD: {os.getcwd()}\n"
            f"History of actions taken so far:\n{history}\n"
            f"Respond with your next tool call (or 'DONE'):"
        )

        payload = {
            "model": MODEL,
            "prompt": full_prompt,
            "stream": False
        }

        # Parse HOST to handle schemes safely
        host_clean = HOST.strip()
        if host_clean.startswith("http://") or host_clean.startswith("https://"):
            req_url = f"{host_clean.rstrip('/')}/api/generate"
        else:
            req_url = f"http://{host_clean}/api/generate"

        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            req_url, 
            data=req_data, 
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        # Setup SSL context if needed
        ssl_context = None
        if req_url.startswith("https://") and not VERIFY_SSL:
            ssl_context = ssl._create_unverified_context()

        try:
            with urllib.request.urlopen(req, context=ssl_context) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                final_cmd = res_data.get("response", "").strip()
        except urllib.error.URLError as e:
            print(f"Error: Connection to model at {HOST} failed: {e.reason}")
            if collect_tools:
                return functioning_tools
            sys.exit(1)
        except Exception as e:
            print(f"Error communicating with model: {str(e)}")
            if collect_tools:
                return functioning_tools
            sys.exit(1)

        if not final_cmd:
            print("Error: Received empty response from model.")
            if collect_tools:
                return functioning_tools
            sys.exit(1)

        if final_cmd == "DONE":
            print("Task completed successfully.")
            if collect_tools:
                return functioning_tools
            return "DONE"

        print(f"Command: {final_cmd}")
        
        # Track steps in thread-local storage for server completions
        if hasattr(current_thread_data, "steps"):
            current_thread_data.steps.append(f"Step {step}: {final_cmd}")

        print("Executing...")
        if final_cmd.startswith("/"):
            output = execute_slash_command(final_cmd)
        else:
            output = execute_tool(final_cmd)
        print(f"Output: {output}")
        print("--------------------------------------")

        # Save functioning tools if needed
        if collect_tools:
            if is_tool_functional(final_cmd, output):
                functioning_tools.append(final_cmd)

        # Update run history for context in the next iteration
        history += f"\nStep {step} Command: {final_cmd}\nResult: {output}"
        step += 1

    print(f"Reached maximum execution limit of {max_steps} steps.")
    if collect_tools:
        return functioning_tools
    return "FAILED"

# --- Skills Handler ---
def activate_skill(name):
    skill_path = os.path.join(SKILLS_DIR, f"{name}.json")
    if not os.path.exists(skill_path):
        print(f"Skill '{name}' does not exist.")
        return f"Skill '{name}' does not exist."
    
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            skill_data = json.load(f)
    except Exception as e:
        print(f"Error loading skill file: {e}")
        return f"Error loading skill file: {e}"
    
    description = skill_data.get("description", "")
    tools = skill_data.get("tools", [])
    
    if tools:
        print(f"Activating skill '{name}': executing saved functioning tools...")
        for i, cmd in enumerate(tools):
            print(f"[{name}] Executing saved tool {i+1}/{len(tools)}: {cmd}")
            if cmd.startswith("/"):
                result = execute_slash_command(cmd)
            else:
                result = execute_tool(cmd)
            print(f"[{name}] Output: {result}")
        return f"Executed saved tools for skill '{name}'"
    else:
        print(f"Activating skill '{name}' for the first time: planning and executing...")
        functioning_tools = run_agent(description, collect_tools=True)
        skill_data["tools"] = functioning_tools
        try:
            with open(skill_path, "w", encoding="utf-8") as f:
                json.dump(skill_data, f, indent=4)
            print(f"Saved {len(functioning_tools)} functioning tools to skill '{name}'.")
        except Exception as e:
            print(f"Error saving compiled skill: {e}")
        return f"Compiled and saved skill '{name}'"

def handle_skill_command(args):
    parts = args.strip().split(maxsplit=1)
    if not parts:
        print("Usage: /skill [list | add <name> <description> | remove <name>]")
        return "Invalid skill command"
        
    subcmd = parts[0].lower()
    subargs = parts[1] if len(parts) > 1 else ""
    
    if subcmd == "list":
        try:
            files = os.listdir(SKILLS_DIR)
            skills = [f[:-5] for f in files if f.endswith(".json")]
            if not skills:
                print("No skills configured.")
                return "No skills configured."
            output = "Available Skills:\n"
            for skill in skills:
                skill_path = os.path.join(SKILLS_DIR, f"{skill}.json")
                try:
                    with open(skill_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    desc = data.get("description", "No description")
                    tools_count = len(data.get("tools", []))
                    output += f"- {skill}: {desc} ({tools_count} saved tools)\n"
                except Exception:
                    output += f"- {skill}: Error reading metadata\n"
            print(output.strip())
            return output.strip()
        except Exception as e:
            print(f"Error listing skills: {e}")
            return f"Error listing skills: {e}"
            
    elif subcmd == "add":
        subparts = subargs.split(maxsplit=1)
        if len(subparts) < 2:
            print("Usage: /skill add <name> <description>")
            return "Invalid add arguments"
        name = subparts[0]
        description = subparts[1]
        
        skill_path = os.path.join(SKILLS_DIR, f"{name}.json")
        skill_data = {
            "description": description,
            "tools": []
        }
        try:
            with open(skill_path, "w", encoding="utf-8") as f:
                json.dump(skill_data, f, indent=4)
            msg = f"Successfully added skill '{name}': {description}"
            print(msg)
            return msg
        except Exception as e:
            print(f"Error adding skill: {e}")
            return f"Error adding skill: {e}"
            
    elif subcmd == "remove":
        if not subargs:
            print("Usage: /skill remove <name>")
            return "Missing skill name"
        name = subargs.strip()
        skill_path = os.path.join(SKILLS_DIR, f"{name}.json")
        if os.path.exists(skill_path):
            try:
                os.remove(skill_path)
                msg = f"Successfully removed skill '{name}'"
                print(msg)
                return msg
            except Exception as e:
                print(f"Error removing skill: {e}")
                return f"Error removing skill: {e}"
        else:
            msg = f"Skill '{name}' does not exist"
            print(msg)
            return msg
    else:
        print(f"Unknown skill sub-command: {subcmd}")
        return f"Unknown skill sub-command: {subcmd}"

# --- Verbal Scheduler Pattern Parser ---
def parse_verbal_time_pattern(pattern):
    pattern = pattern.lower().strip()
    if pattern == "every minute":
        return "* * * * *"
        
    if pattern.startswith("every ") and " minute" in pattern:
        parts = pattern.split()
        try:
            n = int(parts[1])
            return f"*/{n} * * * *"
        except ValueError:
            pass
            
    if pattern.startswith("every ") and " hour" in pattern:
        parts = pattern.split()
        try:
            n = int(parts[1])
            return f"0 */{n} * * *"
        except ValueError:
            pass
            
    if pattern in ("every hour", "hourly"):
        return "0 * * * *"
        
    if pattern in ("every day", "daily"):
        return "0 0 * * *"
        
    def parse_time(time_str):
        time_str = time_str.strip()
        is_pm = "pm" in time_str
        is_am = "am" in time_str
        time_str = time_str.replace("am", "").replace("pm", "").strip()
        
        if ":" in time_str:
            hour_str, min_str = time_str.split(":")
            hour = int(hour_str)
            minute = int(min_str)
        else:
            hour = int(time_str)
            minute = 0
            
        if is_pm and hour < 12:
            hour += 12
        elif is_am and hour == 12:
            hour = 0
        return minute, hour

    if pattern.startswith("every day at ") or pattern.startswith("daily at "):
        time_part = pattern.replace("every day at ", "").replace("daily at ", "")
        try:
            minute, hour = parse_time(time_part)
            return f"{minute} {hour} * * *"
        except Exception:
            pass
            
    weekdays = {
        "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
        "friday": 5, "saturday": 6, "sunday": 0
    }
    for wday, wday_num in weekdays.items():
        prefix = f"every {wday} at "
        if pattern.startswith(prefix):
            time_part = pattern.replace(prefix, "")
            try:
                minute, hour = parse_time(time_part)
                return f"{minute} {hour} * * {wday_num}"
            except Exception:
                pass
                
    if len(pattern.split()) == 5:
        return pattern
        
    raise ValueError(f"Could not parse verbal time pattern: '{pattern}'")

# --- Cron Handler ---
def load_crons():
    if not os.path.exists(CRON_FILE):
        return []
    try:
        with open(CRON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_crons(crons):
    try:
        with open(CRON_FILE, "w", encoding="utf-8") as f:
            json.dump(crons, f, indent=4)
    except Exception as e:
        print(f"Error saving crons: {e}")

def match_cron_field(field, val):
    if field == '*':
        return True
    parts = field.split(',')
    for part in parts:
        if '/' in part:
            subpart, step = part.split('/')
            step = int(step)
            if subpart == '*':
                if val % step == 0:
                    return True
            else:
                start, end = map(int, subpart.split('-'))
                if start <= val <= end and (val - start) % step == 0:
                    return True
        elif '-' in part:
            start, end = map(int, part.split('-'))
            if start <= val <= end:
                return True
        else:
            try:
                if int(part) == val:
                    return True
            except ValueError:
                pass
    return False

def match_cron(pattern, dt):
    fields = pattern.split()
    if len(fields) != 5:
        return False
    min_match = match_cron_field(fields[0], dt.minute)
    hour_match = match_cron_field(fields[1], dt.hour)
    dom_match = match_cron_field(fields[2], dt.day)
    month_match = match_cron_field(fields[3], dt.month)
    cron_wday = (dt.weekday() + 1) % 7
    wday_match = match_cron_field(fields[4], cron_wday)
    return min_match and hour_match and dom_match and month_match and wday_match

def handle_cron_command(args):
    parts = args.strip().split()
    if not parts:
        print("Usage: /cron [list | add <verbal_schedule> <skill_name> | remove <index_or_skill> | daemon]")
        return "Invalid cron command"
        
    subcmd = parts[0].lower()
    
    if subcmd == "list":
        crons = load_crons()
        if not crons:
            print("No cron jobs configured.")
            return "No cron jobs configured."
        output = "Configured Cron Jobs:\n"
        for i, job in enumerate(crons):
            verbal = job.get("verbal", job.get("pattern"))
            output += f"{i+1}. Schedule: '{verbal}' -> Skill: '{job['skill']}'\n"
        print(output.strip())
        return output.strip()
        
    elif subcmd == "add":
        subparts = parts[1:]
        if len(subparts) < 2:
            print("Usage: /cron add <verbal_schedule> <skill_name>")
            return "Invalid add arguments"
            
        skill_name = subparts[-1]
        verbal_pattern = " ".join(subparts[:-1]).strip("'\"")
        
        try:
            cron_pattern = parse_verbal_time_pattern(verbal_pattern)
        except ValueError as e:
            print(f"Error parsing time pattern: {e}")
            return f"Error: {e}"
        
        # Check if skill exists (warn user if not, but still allow scheduling)
        skill_path = os.path.join(SKILLS_DIR, f"{skill_name}.json")
        if not os.path.exists(skill_path):
            print(f"Warning: Skill '{skill_name}' does not exist yet. You should create it using '/skill add {skill_name} <description>'.")
            
        crons = load_crons()
        crons.append({
            "pattern": cron_pattern,
            "verbal": verbal_pattern,
            "skill": skill_name
        })
        save_crons(crons)
        msg = f"Successfully added cron job: '{verbal_pattern}' -> '{skill_name}'"
        print(msg)
        return msg
        
    elif subcmd == "remove":
        if len(parts) < 2:
            print("Usage: /cron remove <skill_name_or_index>")
            return "Missing remove argument"
        target = " ".join(parts[1:])
        crons = load_crons()
        new_crons = []
        removed = False
        
        # Try numeric index (1-based)
        try:
            idx = int(target) - 1
            if 0 <= idx < len(crons):
                removed_job = crons.pop(idx)
                save_crons(crons)
                msg = f"Removed cron job: '{removed_job.get('verbal', removed_job['pattern'])}' -> '{removed_job['skill']}'"
                print(msg)
                return msg
        except ValueError:
            pass
            
        # Match by skill name
        for job in crons:
            if job["skill"] == target:
                removed = True
            else:
                new_crons.append(job)
        if removed:
            save_crons(new_crons)
            msg = f"Removed all cron jobs for skill: '{target}'"
            print(msg)
            return msg
        else:
            msg = f"No cron job matched '{target}'"
            print(msg)
            return msg
            
    elif subcmd == "daemon":
        run_cron_daemon()
        return "Cron daemon stopped."
    else:
        print(f"Unknown cron sub-command: {subcmd}")
        return f"Unknown cron sub-command: {subcmd}"

def run_cron_daemon(background=False):
    def daemon_loop():
        print("Cron scheduler daemon started.")
        while True:
            try:
                now = datetime.datetime.now()
                sleep_sec = 60 - now.second - now.microsecond/1000000.0
                time.sleep(sleep_sec)
                
                now = datetime.datetime.now()
                crons = load_crons()
                for job in crons:
                    pattern = job.get("pattern")
                    skill_name = job.get("skill")
                    if pattern and skill_name and match_cron(pattern, now):
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Cron trigger: activating skill '{skill_name}'")
                        t = threading.Thread(target=activate_skill, args=(skill_name,))
                        t.daemon = True
                        t.start()
            except Exception as e:
                print(f"Error in cron daemon loop: {e}")
                time.sleep(10)
                
    if background:
        t = threading.Thread(target=daemon_loop)
        t.daemon = True
        t.start()
        print("Cron scheduler daemon started in background thread.")
    else:
        daemon_loop()

# --- OpenAI Server Handler ---
class OpenAIHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, data):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        except Exception as e:
            print(f"Error sending JSON response: {e}")
            
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        
    def do_GET(self):
        if self.path in ("/v1/models", "/v1/models/"):
            self._send_json(200, {
                "object": "list",
                "data": [
                    {
                        "id": "openagent",
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "openagent"
                    }
                ]
            })
        else:
            self._send_json(404, {"error": "Not Found"})
            
    def do_POST(self):
        if self.path in ("/v1/chat/completions", "/v1/chat/completions/"):
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"error": f"Invalid JSON: {str(e)}"})
                return
            
            messages = data.get("messages", [])
            if not messages:
                self._send_json(400, {"error": "No messages provided"})
                return
            
            user_msg = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    user_msg = msg.get("content", "")
                    break
                    
            if not user_msg:
                self._send_json(400, {"error": "No user message found"})
                return
            
            print(f"[Server] Received request: '{user_msg}'")
            
            # Setup thread-local tracking variables
            current_thread_data.yaps = []
            current_thread_data.steps = []
            
            try:
                if user_msg.startswith("/"):
                    res = execute_slash_command(user_msg)
                    response_content = res
                else:
                    res = run_agent(user_msg)
                    yaps = current_thread_data.yaps
                    if yaps:
                        response_content = "\n".join(yaps)
                    else:
                        steps_str = "\n".join(current_thread_data.steps)
                        response_content = f"Task completed: {res}\nSteps taken:\n{steps_str}"
            except Exception as e:
                response_content = f"Error executing task: {str(e)}"
                
            response_data = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "openagent",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_content
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }
            self._send_json(200, response_data)
        else:
            self._send_json(404, {"error": "Not Found"})

def start_openai_server():
    port = int(os.environ.get("AGENT_PORT", os.environ.get("PORT", "5001")))
    server = ThreadingHTTPServer(("0.0.0.0", port), OpenAIHandler)
    print(f"OpenAgent OpenAI-compatible endpoint listening on http://0.0.0.0:{port}")
    
    # Start the cron scheduler daemon in background thread
    run_cron_daemon(background=True)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()
    return "Server stopped"

# --- Slash Command Router ---
def execute_slash_command(cmd_str):
    parts = cmd_str.strip().split(maxsplit=1)
    if not parts:
        return "Error: Empty command"
    cmd = parts[0][1:] if parts[0].startswith("/") else parts[0]
    args = parts[1] if len(parts) > 1 else ""
    
    if cmd == "cron":
        return handle_cron_command(args)
    elif cmd == "skill":
        return handle_skill_command(args)
    elif cmd in ("serve", "server"):
        return start_openai_server()
    else:
        # Check if it's a skill
        skill_path = os.path.join(SKILLS_DIR, f"{cmd}.json")
        if os.path.exists(skill_path):
            return activate_skill(cmd)
        else:
            # If the user ran `/<name> <description>`, automatically create and run the skill!
            if args:
                skill_path = os.path.join(SKILLS_DIR, f"{cmd}.json")
                skill_data = {
                    "description": args,
                    "tools": []
                }
                try:
                    with open(skill_path, "w", encoding="utf-8") as f:
                        json.dump(skill_data, f, indent=4)
                    print(f"Created new skill '{cmd}' with description: {args}")
                    return activate_skill(cmd)
                except Exception as e:
                    return f"Error creating skill '{cmd}': {e}"
            else:
                try:
                    files = os.listdir(SKILLS_DIR)
                    skills = [f[:-5] for f in files if f.endswith(".json")]
                except Exception:
                    skills = []
                msg = f"Unknown command or skill: '{cmd}'. Available skills: {skills}"
                print(msg)
                return msg

# --- CLI Execution Entry ---
def main():
    if len(sys.argv) < 2:
        print("Usage: oa.py \"<request_or_slash_command>\"")
        print("\nServer Execution Mode:")
        print("  --server, --serve, server, serve   - Start the OpenAI-compatible API server & Cron scheduler")
        print("\nSlash Commands:")
        print("  /skill list                        - List all available skills")
        print("  /skill add <name> <description>    - Add a new skill")
        print("  /skill remove <name>               - Remove a skill")
        print("  /<skill_name>                      - Activate a skill (compiles functioning tools on first run)")
        print("  /cron list                         - List all cron jobs")
        print("  /cron add <verbal_schedule> <skill>- Add a new cron job using natural verbal language")
        print("  /cron remove <index_or_skill>      - Remove a cron job")
        print("  /cron daemon                       - Start the cron scheduler daemon in foreground")
        sys.exit(1)

    prompt_request = sys.argv[1]
    
    if prompt_request in ("--server", "--serve", "server", "serve", "/serve", "/server"):
        start_openai_server()
    elif prompt_request.startswith("/"):
        execute_slash_command(prompt_request)
    else:
        run_agent(prompt_request)

if __name__ == "__main__":
    main()
