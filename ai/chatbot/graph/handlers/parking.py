from chatbot.graph.state import ChatState

from chatbot.rag.utils import get_query_embedding, perform_vector_search, close_mongo_client
from chatbot.rag.config import RAG_SEARCH_CONFIG, common_llm_rag_caller
from chatbot.rag.config import client

import os
import requests
from datetime import datetime
import re
from dotenv import load_dotenv
import json

# 새로운 LLM 파싱 함수를 임포트합니다.
from chatbot.rag.parking_fee_helper import _parse_parking_fee_query_with_llm
from chatbot.rag.parking_walk_time_helper import _parse_parking_walk_time_query_with_llm
from chatbot.rag.llm_tools import _format_and_style_with_llm

load_dotenv()

SERVICE_KEY = os.getenv("SERVICE_KEY")
if not SERVICE_KEY:
    raise ValueError("SERVICE_KEY 환경 변수가 설정되지 않았습니다.")

# 주차장 현황 API URL
API_URL = "http://apis.data.go.kr/B551177/StatusOfParking/getTrackingParking"

def parking_fee_info_handler(state: ChatState) -> ChatState:
    """
    'parking_fee_info' 의도에 대한 RAG 기반 핸들러.
    사용자 쿼리를 기반으로 MongoDB에서 주차 요금 및 할인 정책 정보를 검색하고 답변을 생성합니다.
    여러 주차 요금 토픽에 대한 복합 질문도 처리할 수 있도록 개선되었습니다.
    """
    # 📌 수정된 부분: rephrased_query를 먼저 확인하고, 없으면 user_input을 사용합니다.
    query_to_process = state.get("rephrased_query") or state.get("user_input", "")
    intent_name = state.get("intent", "parking_fee_info")
    slots = state.get("slots", [])
    
    if not query_to_process:
        print("디버그: 사용자 쿼리가 비어 있습니다.")
        return {**state, "response": "죄송합니다. 질문 내용을 파악할 수 없습니다. 다시 질문해주세요."}

    print(f"\n--- {intent_name.upper()} 핸들러 실행 ---")
    print(f"디버그: 핸들러가 처리할 최종 쿼리 - '{query_to_process}'")

    # 🚀 최적화: slot 정보 우선 활용, 없으면 LLM fallback
    fee_topics = [word for word, slot in slots if slot in ['B-fee_topic', 'I-fee_topic']]
    vehicle_types = [word for word, slot in slots if slot in ['B-vehicle_type', 'I-vehicle_type']]
    parking_areas = [word for word, slot in slots if slot in ['B-parking_area', 'I-parking_area']]
    time_periods = [word for word, slot in slots if slot in ['B-time_period', 'I-time_period']]
    
    search_queries = []
    if fee_topics or vehicle_types or parking_areas or time_periods:
        print(f"디버그: ⚡ slot에서 주차 정보 추출 - 주제:{fee_topics}, 차량:{vehicle_types}, 구역:{parking_areas}, 시간:{time_periods}")
        
        # slot 조합으로 구체적인 검색 쿼리 생성
        if len(fee_topics) > 1:
            # 여러 fee_topic이 있으면 각각을 개별 쿼리로 처리
            for topic in fee_topics:
                base_query = f"{topic} 주차 요금"
                if vehicle_types:
                    base_query += f" {' '.join(vehicle_types)}"
                if parking_areas:
                    base_query += f" {' '.join(parking_areas)}"
                search_queries.append(base_query)
        else:
            # 단일 쿼리 생성
            all_keywords = fee_topics + vehicle_types + parking_areas + time_periods
            base_query = " ".join(all_keywords) if all_keywords else "주차 요금"
            search_queries = [base_query]
        
        print(f"디버그: ⚡ slot 기반으로 생성된 검색 쿼리: {search_queries}")
    else:
        print("디버그: slot에 주차 정보 없음, LLM으로 fallback")
        # 기존 LLM 방식 사용
        if len(slots) > 0:  # 다른 slot이라도 있으면 LLM 시도
            parsed_queries = _parse_parking_fee_query_with_llm(query_to_process)
            if parsed_queries and parsed_queries.get("requests"):
                search_queries = [req.get("query") for req in parsed_queries["requests"]]
        
        if not search_queries:
            search_queries = [query_to_process]
            print("디버그: 복합 질문으로 파악되지 않아 최종 쿼리로 검색을 시도합니다.")

    # RAG_SEARCH_CONFIG에서 현재 의도에 맞는 설정 가져오기
    rag_config = RAG_SEARCH_CONFIG.get(intent_name, {})
    collection_name = rag_config.get("collection_name")
    vector_index_name = rag_config.get("vector_index_name")
    intent_description = rag_config.get("description", intent_name)
    query_filter = rag_config.get("query_filter")

    if not (collection_name and vector_index_name):
        error_msg = f"죄송합니다. '{intent_name}' 의도에 대한 정보 검색 설정을 찾을 수 없거나 인덱스 이름이 누락되었습니다."
        print(f"디버그: {error_msg}")
        return {**state, "response": error_msg}

    all_retrieved_docs_text = []
    try:
        for query in search_queries:
            print(f"디버그: '{query}'에 대해 검색 시작...")
            
            # 📌 수정된 부분: 검색을 위해 query_embedding에 query를 전달합니다.
            query_embedding = get_query_embedding(query)
            retrieved_docs_text = perform_vector_search(
                query_embedding,
                collection_name=collection_name,
                vector_index_name=vector_index_name,
                query_filter=query_filter,
                top_k=5
            )
            all_retrieved_docs_text.extend(retrieved_docs_text)
            
        print(f"디버그: MongoDB에서 총 {len(all_retrieved_docs_text)}개 문서 검색 완료.")

        if not all_retrieved_docs_text:
            print("디버그: 벡터 검색 결과, 관련 문서가 없습니다.")
            return {**state, "response": "죄송합니다. 요청하신 주차 요금 정보를 찾을 수 없습니다."}

        context_for_llm = "\n\n".join(all_retrieved_docs_text)
        print(f"디버그: LLM에 전달될 최종 컨텍스트 길이: {len(context_for_llm)}자.")
        
        # 📌 수정된 부분: common_llm_rag_caller에 query_to_process를 전달합니다.
        final_response = common_llm_rag_caller(query_to_process, context_for_llm, intent_description, intent_name)
        
        return {**state, "response": final_response}

    except Exception as e:
        error_msg = f"죄송합니다. 정보를 검색하는 중 오류가 발생했습니다: {e}"
        print(f"디버그: {error_msg}")
        return {**state, "response": error_msg}

def parking_congestion_prediction_handler(state: ChatState) -> ChatState:
    return {**state, "response": "추후 제공할 기능입니다! 현재는 실시간 주차장 현황에 대해서만 제공하고 있습니다."}

def parking_location_recommendation_handler(state: ChatState) -> ChatState:
    """
    'parking_location_recommendation' 의도에 대한 RAG 기반 핸들러.
    사용자 쿼리를 기반으로 MongoDB에서 주차장 위치 정보를 검색하고 답변을 생성합니다.
    여러 주차장 위치에 대한 복합 질문도 처리할 수 있도록 개선되었습니다.
    """
    # 📌 수정된 부분: rephrased_query를 먼저 확인하고, 없으면 user_input을 사용합니다.
    query_to_process = state.get("rephrased_query") or state.get("user_input", "")
    intent_name = state.get("intent", "parking_location_recommendation")
    slots = state.get("slots", [])

    if not query_to_process:
        print("디버그: 사용자 쿼리가 비어 있습니다.")
        return {**state, "response": "죄송합니다. 질문 내용을 파악할 수 없습니다. 다시 질문해주세요."}

    print(f"\n--- {intent_name.upper()} 핸들러 실행 ---")
    print(f"디버그: 핸들러가 처리할 최종 쿼리 - '{query_to_process}'")

    # 🚀 최적화: slot 정보 우선 활용, 없으면 LLM fallback
    parking_lots = [word for word, slot in slots if slot in ['B-parking_lot', 'I-parking_lot']]
    parking_areas = [word for word, slot in slots if slot in ['B-parking_area', 'I-parking_area']]
    terminals = [word for word, slot in slots if slot in ['B-terminal', 'I-terminal']]
    
    search_keywords = []
    if parking_lots or parking_areas or terminals:
        print(f"디버그: ⚡ slot에서 주차 위치 정보 추출 - 주차장:{parking_lots}, 구역:{parking_areas}, 터미널:{terminals}")
        
        # slot 조합으로 검색 키워드 생성
        all_keywords = parking_lots + parking_areas + terminals
        search_keywords = list(set(all_keywords)) if all_keywords else [query_to_process]
        print(f"디버그: ⚡ slot 기반 검색 키워드: {search_keywords}")
    else:
        print("디버그: slot에 주차 위치 정보 없음, 전체 쿼리로 검색")
        search_keywords = [query_to_process]

    rag_config = RAG_SEARCH_CONFIG.get(intent_name, {})
    collection_name = rag_config.get("collection_name")
    vector_index_name = rag_config.get("vector_index_name")
    intent_description = rag_config.get("description", intent_name)
    query_filter = rag_config.get("query_filter")

    if not (collection_name and vector_index_name):
        error_msg = f"죄송합니다. '{intent_name}' 의도에 대한 정보 검색 설정을 찾을 수 없거나 인덱스 이름이 누락되었습니다."
        print(f"디버그: {error_msg}")
        return {**state, "response": error_msg}

    all_retrieved_docs_text = []
    try:
        for keyword in search_keywords:
            print(f"디버그: '{keyword}'에 대해 검색 시작...")

            # 📌 수정된 부분: 검색을 위해 query_embedding에 keyword를 전달합니다.
            query_embedding = get_query_embedding(keyword)
            retrieved_docs_text = perform_vector_search(
                query_embedding,
                collection_name=collection_name,
                vector_index_name=vector_index_name,
                query_filter=query_filter,
                top_k=5
            )
            all_retrieved_docs_text.extend(retrieved_docs_text)

        print(f"디버그: MongoDB에서 총 {len(all_retrieved_docs_text)}개 문서 검색 완료.")
        
        if not all_retrieved_docs_text:
            return {**state, "response": "죄송합니다. 요청하신 주차장 위치 정보를 찾을 수 없습니다."}

        context_for_llm = "\n\n".join(all_retrieved_docs_text)
        print(f"디버그: LLM에 전달될 최종 컨텍스트 길이: {len(context_for_llm)}자.")
        
        # 📌 수정된 부분: common_llm_rag_caller에 query_to_process를 전달합니다.
        final_response = common_llm_rag_caller(query_to_process, context_for_llm, intent_description, intent_name)

        return {**state, "response": final_response}

    except Exception as e:
        error_msg = f"죄송합니다. 정보를 검색하는 중 오류가 발생했습니다: {e}"
        print(f"디버그: {error_msg}")
        return {**state, "response": error_msg}

def parking_availability_query_handler(state: ChatState) -> ChatState:
    """
    'parking_availability_query' 의도에 대한 RAG 기반 핸들러.
    API를 호출하여 주차장 이용 가능 여부를 확인하고 답변을 생성합니다.
    """
    # 📌 수정된 부분: rephrased_query를 먼저 확인하고, 없으면 user_input을 사용합니다.
    query_to_process = state.get("rephrased_query") or state.get("user_input", "")
    intent_name = state.get("intent", "parking_availability_query")
    
    if not query_to_process:
        print("디버그: 사용자 쿼리가 비어 있습니다.")
        return {**state, "response": "죄송합니다. 질문 내용을 파악할 수 없습니다. 다시 질문해주세요."}

    print(f"\n--- {intent_name.upper()} 핸들러 실행 ---")
    print(f"디버그: 핸들러가 처리할 최종 쿼리 - '{query_to_process}'")
    
    params = {
        "serviceKey": SERVICE_KEY,
        "type": "json",
        "numOfRows": 1000,
        "pageNo": 1,
    }
    
    try:
        response = requests.get(API_URL, params=params)
        response.raise_for_status()
        
        print(f"디버그: API 응답 텍스트: {response.text[:200]}")  # 처음 200자만 출력
        print(f"디버그: API 응답 상태 코드: {response.status_code}")
        
        response_data = response.json()
        print(response_data)
        items_container = response_data.get("response", {}).get("body", {}).get("items", {})
        if not items_container:
            response_text = "혼잡도 예측 정보를 찾을 수 없습니다. API 응답이 비어있거나 형식이 다릅니다."
            return {**state, "response": response_text}
        
        items = items_container.get("item", []) if isinstance(items_container, dict) else items_container
        if not items:
            response_text = "혼잡도 예측 정보를 찾을 수 없습니다. API 응답 데이터가 비어있습니다."
            return {**state, "response": response_text}
        if isinstance(items, dict): items = [items]
        
        # 주차 가능 대수 계산 및 마이너스 값 처리
        for item in items:
            available_spots = int(item['parkingarea']) - int(item['parking'])
            item['parking'] = str(max(0, available_spots))  # 마이너스면 0으로 설정
        
        print(items)
        # 📌 수정된 부분: 프롬프트에 query_to_process를 추가
        prompt_template = (
            "당신은 인천국제공항의 정보를 제공하는 친절하고 유용한 챗봇입니다. "
            "사용자 질문에 다음 정보를 바탕으로 답변해주세요.\n"
            "사용자 질문: {user_query}\n"
            "검색된 정보: {items}\n"
            "T1은 인천국제공항 제1여객터미널, T2는 제2여객터미널입니다. "
            "datetmp은 YYYY-MM-DD HH:MM:SS 형식입니다. 주차장 상태를 마지막으로 확인한 시간입니다. 이 시간을 가장 먼저 언급하세요. "
            "parking은 주차 가능 대수입니다. parking이 0이면 '만차'라고 표시해주세요.\n"
            "\n"
            "**답변 형식:**\n"
            "1. 먼저 확인 시간을 언급\n"
            "2. ## T1 (제1여객터미널) 섹션으로 T1 주차장들을 모두 나열\n"
            "3. ## T2 (제2여객터미널) 섹션으로 T2 주차장들을 모두 나열\n"
            "4. 각 주차장은 '- **주차장명**: 주차 가능 대수 **N**대 (또는 **만차**)' 형식으로 출력\n"
        )

        final_prompt = f"{prompt_template}"

        formatted_prompt = final_prompt.format(user_query=query_to_process, items=json.dumps(items, ensure_ascii=False, indent=2))
        
        llm_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": formatted_prompt}
            ],
            temperature=0.5,
            max_tokens=800
        )
        plain_text_response = llm_response.choices[0].message.content
        styled_response = _format_and_style_with_llm(plain_text_response, intent_name)
        
    except requests.RequestException as e:
        print(f"디버그: API 호출 중 오류 발생 - {e}")
        styled_response = "주차장 이용 가능 여부를 가져오는 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요."
    except Exception as e:
        print(f"디버그: 응답 처리 중 오류 발생 - {e}")
        styled_response = "주차장 현황 정보를 처리하는 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요."

    return {**state, "response": styled_response}

def parking_walk_time_info_handler(state: ChatState) -> ChatState:
    """
    'parking_walk_time_info' 의도에 대한 RAG 기반 핸들러.
    사용자 쿼리를 기반으로 MongoDB에서 주차장 도보 시간 정보를 검색하고 답변을 생성합니다.
    복합 질문(여러 출발지-도착지 쌍)도 처리할 수 있도록 개선되었습니다.
    """
    # 📌 수정된 부분: rephrased_query를 먼저 확인하고, 없으면 user_input을 사용합니다.
    query_to_process = state.get("rephrased_query") or state.get("user_input", "")
    intent_name = state.get("intent", "parking_walk_time_info")

    if not query_to_process:
        print("디버그: 사용자 쿼리가 비어 있습니다.")
        return {**state, "response": "죄송합니다. 질문 내용을 파악할 수 없습니다. 다시 질문해주세요."}

    print(f"\n--- {intent_name.upper()} 핸들러 실행 ---")
    print(f"디버그: 핸들러가 처리할 최종 쿼리 - '{query_to_process}'")

    # 🚀 최적화: slot 정보 우선 활용, 없으면 LLM fallback
    slots = state.get("slots", [])
    parking_lots = [word for word, slot in slots if slot in ['B-parking_lot', 'I-parking_lot']]
    parking_areas = [word for word, slot in slots if slot in ['B-parking_area', 'I-parking_area']]
    terminals = [word for word, slot in slots if slot in ['B-terminal', 'I-terminal']]
    locations = [word for word, slot in slots if slot in ['B-location', 'I-location']]
    
    search_queries = []
    if parking_lots or parking_areas or terminals or locations:
        print(f"디버그: ⚡ slot에서 도보 시간 정보 추출 - 주차장:{parking_lots}, 구역:{parking_areas}, 터미널:{terminals}, 위치:{locations}")
        
        # slot 조합으로 도보 시간 검색 쿼리 생성
        for parking in (parking_lots or ['주차장']):
            for destination in (terminals + locations or ['터미널']):
                search_queries.append(f"{parking}에서 {destination}까지 도보 시간")
        
        # 중복 제거
        search_queries = list(set(search_queries)) if search_queries else [query_to_process]
        print(f"디버그: ⚡ slot 기반으로 생성된 검색 쿼리: {search_queries}")
    else:
        print("디버그: slot에 도보 시간 정보 없음, LLM으로 fallback")
        parsed_queries = _parse_parking_walk_time_query_with_llm(query_to_process)
        if parsed_queries and parsed_queries.get("requests"):
            search_queries = [req.get("query") for req in parsed_queries["requests"]]
        
        if not search_queries:
            search_queries = [query_to_process]
            print("디버그: 복합 질문으로 파악되지 않아 최종 쿼리로 검색을 시도합니다.")

    rag_config = RAG_SEARCH_CONFIG.get(intent_name, {})
    collection_name = rag_config.get("collection_name")
    vector_index_name = rag_config.get("vector_index_name")
    intent_description = rag_config.get("description", intent_name)
    query_filter = rag_config.get("query_filter")

    if not (collection_name and vector_index_name):
        error_msg = f"죄송합니다. '{intent_name}' 의도에 대한 정보 검색 설정을 찾을 수 없거나 인덱스 이름이 누락되었습니다."
        print(f"디버그: {error_msg}")
        return {**state, "response": error_msg}

    all_retrieved_docs_text = []
    try:
        for query in search_queries:
            print(f"디버그: '{query}'에 대해 검색 시작...")
            
            # 📌 수정된 부분: 검색을 위해 query_embedding에 query를 전달합니다.
            query_embedding = get_query_embedding(query)
            retrieved_docs_text = perform_vector_search(
                query_embedding,
                collection_name=collection_name,
                vector_index_name=vector_index_name,
                query_filter=query_filter,
                top_k=5
            )
            all_retrieved_docs_text.extend(retrieved_docs_text)
            
        print(f"디버그: MongoDB에서 총 {len(all_retrieved_docs_text)}개 문서 검색 완료.")

        if not all_retrieved_docs_text:
            print("디버그: 필터링 및 벡터 검색 결과, 관련 문서가 없습니다.")
            return {**state, "response": "죄송합니다. 해당 주차장 도보 시간 정보를 찾을 수 없습니다. 혹시 이용하시는 항공사나 카운터 번호를 알고 계시면 더 정확한 정보를 찾아드릴 수 있습니다."}

        context_for_llm = "\n\n".join(all_retrieved_docs_text)
        print(f"디버그: LLM에 전달될 최종 컨텍스트 길이: {len(context_for_llm)}자.")

        # 📌 수정된 부분: common_llm_rag_caller에 query_to_process를 전달합니다.
        final_response = common_llm_rag_caller(query_to_process, context_for_llm, intent_description, intent_name)

        return {**state, "response": final_response}

    except Exception as e:
        error_msg = f"죄송합니다. 정보를 검색하는 중 오류가 발생했습니다: {e}"
        print(f"디버그: {error_msg}")
        return {**state, "response": error_msg}