from src.agent.tools.quote_tools import create_quote_draft, request_quote_approval
from src.agent.tools.vehicle_tools import get_vehicle, search_vehicles
from src.agent.tools.report_tools import list_vehicle_reports, read_vehicle_report
from src.agent.tools.report_analysis_tools import finalize_vehicle_report
from src.agent.tools.memory_tools import propose_user_memory
from src.agent.tools.web_tools import search_web


AGENT_TOOLS = [get_vehicle, search_vehicles, create_quote_draft, request_quote_approval,
               list_vehicle_reports, read_vehicle_report, finalize_vehicle_report, propose_user_memory, search_web]

READ_ONLY_TOOLS = frozenset({"get_vehicle", "search_vehicles", "create_quote_draft",
                             "list_vehicle_reports", "read_vehicle_report", "finalize_vehicle_report", "search_web"})
