"""What an agent is to the kit (layer 5, with stops/): the adapter interface, and the stand-ins an agent under test
talks to: the scripted model, the MCP test server and the worker stand-in.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_STANDIN = os.path.join(HERE, 'model_standin.py')     # run as a program: python3 -I -B MODEL_STANDIN ...
MCP_SERVER = os.path.join(HERE, 'mcp_server.py')           # the same
