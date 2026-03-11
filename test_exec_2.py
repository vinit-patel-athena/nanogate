from nanobot.agent.tools.shell import ExecTool

try:
    c = ExecTool()
    print("ExecTool instantiated successfully")
except Exception as e:
    print("Error:", e)
