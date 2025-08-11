import importlib
import pkgutil
from functools import partial
from langgraph.graph import StateGraph, END

from chatbot.graph.state import ChatState
from chatbot.graph.nodes.classifiy_intent import classify_intent
from chatbot.graph.nodes.complex_handler import handle_complex_intent
from chatbot.graph.nodes.llm_verify_intent import llm_verify_intent_node
import chatbot.graph.handlers


def build_chat_graph():
    builder = StateGraph(ChatState)
    handlers = {}
    supported_intents = []

    # classify_intent 노드를 가장 먼저 추가
    builder.add_node("classify_intent", classify_intent)

    # 핸들러 노드들을 동적으로 추가하고 엣지를 연결
    for importer, modname, ispkg in pkgutil.iter_modules(chatbot.graph.handlers.__path__):
        if modname.startswith("__"):
            continue
        
        module = importlib.import_module(f"chatbot.graph.handlers.{modname}")
        for attribute_name in dir(module):
            attribute = getattr(module, attribute_name)
            if callable(attribute) and attribute_name.endswith("_handler"):
                node_name = attribute_name
                builder.add_node(node_name, attribute)
                builder.add_edge(node_name, END)
                handlers[node_name] = attribute
                supported_intents.append(node_name.replace("_handler", ""))

    # 복합 의도 처리 노드 추가
    complex_handler_node = partial(handle_complex_intent, handlers=handlers, supported_intents=supported_intents)
    builder.add_node("handle_complex_intent", complex_handler_node)
    builder.add_edge("handle_complex_intent", END)

    # LLM 검증 노드 추가
    builder.add_node("llm_verify_intent", llm_verify_intent_node)
    
    def route_final_intent_to_handler(state):
        final_intent = state.get("intent")
        if final_intent:
            if final_intent == "complex_intent":
                return "handle_complex_intent"
            node_name = f"{final_intent}_handler"
            if node_name in handlers:
                return node_name
        return "fallback_handler"
    
    def route_after_initial_classification(state: ChatState) -> str:
        top_k_intents = state.get('top_k_intents_and_probs', [])
        slots = state.get("slots", [])
        user_query = state.get("user_input", "")

        # 📌 수정된 부분: 이전 대화 감지 로직을 최상단으로 이동
        if len(state.get("messages", [])) > 1:
            print("DEBUG: 이전 대화 감지 -> llm_verify_intent로 라우팅")
            return "llm_verify_intent"

        # 1. 복합 의도 감지 (기존 로직 그대로)
        slot_groups = {
            # 주차 관련 의도들
            'parking_fee_info': {'B-parking_type', 'B-parking_lot', 'B-fee_topic', 'B-vehicle_type', 'B-payment_method'},
            'parking_availability_query': {'B-parking_type', 'B-parking_lot', 'B-availability_status'},
            'parking_location_recommendation': {'B-parking_lot', 'B-location_keyword'},
            'parking_congestion_prediction': {'B-congestion_topic'},
            
            # 항공편 관련 의도들
            'flight_info': {'B-airline_flight', 'B-airline_name', 'B-airport_name', 'B-airport_code', 'B-destination', 'B-departure_airport', 'B-arrival_airport', 'B-gate', 'B-flight_status'},
            'airline_info_query': {'B-airline_name', 'B-airline_info'},
            'baggage_claim_info': {'B-luggage_term', 'B-baggage_issue'},
            'baggage_rule_query': {'B-baggage_type', 'B-rule_type', 'B-item'},
            
            # 공항 시설 및 정책 관련 의도들
            'facility_guide': {'B-facility_name', 'B-location_keyword'},
            'airport_info': {'B-airport_name', 'B-airport_code'},
            'immigration_policy': {'B-organization', 'B-person_type', 'B-rule_type', 'B-document'},
            
            # 환승 관련 의도들
            'transfer_info': {'B-transfer_topic'},
            'transfer_route_guide': {'B-transfer_topic'},
            
            # 날씨 및 혼잡도 관련 의도들
            'airport_weather_current': {'B-weather_topic'},
            'airport_congestion_prediction': {'B-congestion_topic'},
            
            # 공통 슬롯 그룹 (복합 의도 감지에서 제외)
            'time_general': {'B-date', 'B-time', 'B-vague_time', 'B-season', 'B-day_of_week', 'B-relative_time', 'B-minute', 'B-hour', 'B-time_period'},
            'general_topic': {'B-topic'}
        }
        found_groups = set()
        for _, tag in slots:
            if tag.startswith('B-'):
                for group_name, tags in slot_groups.items():
                    if tag in tags:
                        found_groups.add(group_name)
        
        specific_groups = found_groups - {'general_topic'}
        if len(specific_groups) > 1:
            print("DEBUG: 슬롯 기반 복합 의도 감지 -> handle_complex_intent로 라우팅")
            return "handle_complex_intent"
            
        # 2. 단일 의도 신뢰도 기반 라우팅
        top_intent, top_conf = top_k_intents[0] if top_k_intents else ("default", 0.0)
        
        if top_conf >= 0.9:
            print(f"DEBUG: 높은 신뢰도 단일 의도 감지 -> {top_intent}_handler로 바로 라우팅")
            return f"{top_intent}_handler"
        else:
            print("DEBUG: 낮은 신뢰도 또는 모호한 의도 감지 -> llm_verify_intent로 라우팅")
            return "llm_verify_intent"

    # 그래프의 시작점과 엣지 연결
    builder.set_entry_point("classify_intent")
    
    all_nodes = list(handlers.keys()) + ["handle_complex_intent", "llm_verify_intent"]
    
    builder.add_conditional_edges(
        "classify_intent",
        route_after_initial_classification,
        all_nodes
    )
    
    builder.add_conditional_edges(
        "llm_verify_intent",
        route_final_intent_to_handler,
        all_nodes
    )

    return builder.compile()