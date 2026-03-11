from nanobot.agent.tools.shell import ExecTool
class CustomExec(ExecTool):
    def __init__(self):
        super().__init__()

print("Instantiating...")
CustomExec()
print("Done!")
