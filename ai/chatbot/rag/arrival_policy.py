from sentence_transformers import SentenceTransformer
from .client import get_model, query_vector_store

def get_arrival_policy_info(user_input: str) -> list:
    model = get_model()
    query_embedding = model.encode(user_input).tolist()
    
    print(f"\n--- [Arrival Policy] 처리 중 쿼리: '{user_input}' ---")

    # 1. 'Country' 컬렉션에서 정보 검색
    print(f"  > 'Country' 컬렉션 검색 시작...")
    country_results = query_vector_store("Country", query_embedding, top_k=2) 
    print(f"  > 'Country' 컬렉션 검색 결과 ({len(country_results)}개):")
    for i, res in enumerate(country_results):
        print(f"    - Country Result {i+1}: {res.get('country_name_kor')}, Visa: {res.get('visa_required')}, Entry: {res.get('entry_requirement')}")
    
    # 2. 'AirportProcedure' 컬렉션에서 입국 절차 정보 검색
    print(f"  > 'AirportProcedure' 컬렉션 검색 시작...")
    raw_procedure_results = query_vector_store("AirportProcedure", query_embedding, top_k=5) 
    print(f"  > 'AirportProcedure' 컬렉션 원본 검색 결과 ({len(raw_procedure_results)}개):")
    for i, res in enumerate(raw_procedure_results):
        print(f"    - Procedure Raw Result {i+1}: Type: {res.get('procedure_type')}, Desc: {res.get('description')[:50]}...") # 설명의 일부만 출력
    
    # 3. AirportProcedure 결과 중 'procedure_type'이 '입국'인 문서만 필터링
    print(f"  > 'AirportProcedure' 결과 '입국' 유형으로 필터링 중...")
    filtered_procedure_results = []
    for doc in raw_procedure_results:
        if doc.get('procedure_type') == '입국':
            filtered_procedure_results.append(doc)
            # 필터링 통과한 문서의 세부 정보 출력
            print(f"    - FILTERED_IN: Type: {doc.get('procedure_type')}, Desc: {doc.get('description')[:50]}...")
        else:
            # 필터링에 실패한 문서의 유형과 이유 출력
            print(f"    - FILTERED_OUT: Type: {doc.get('procedure_type')}, Desc: {doc.get('description')[:50]}...")
    
    # 4. 두 검색 결과를 결합하여 반환
    # 여기서는 Country 결과를 우선하고, 그 다음 필터링된 AirportProcedure 결과를 추가합니다.
    combined_results = country_results + filtered_procedure_results[:3] 
    
    print(f"--- [Arrival Policy] 총 결합된 문서 수: {len(combined_results)} ---")
    return combined_results

# --- 테스트 코드 (동일) ---
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    test_queries = [
        "미국 입국 절차 알려줘",
        "일본 갈 때 비자 필요해?",
        "공항에 도착하면 어떻게 해야 돼?", # 이 쿼리가 중요합니다.
        "한국으로 입국하는 절차 알려줘", # 이 쿼리도 중요합니다.
        "환승 절차는 어떻게 돼?" 
    ]

    for query in test_queries:
        retrieved_docs = get_arrival_policy_info(query)
        # 최종 결과 출력은 이전과 동일하게 유지
        if retrieved_docs:
            print(f"\n✔️ 최종 검색 결과 ({len(retrieved_docs)}개):")
            for idx, doc in enumerate(retrieved_docs, 1):
                print(f"  📦 최종 결과 {idx}")
                if doc.get('country_name_kor'):
                    print(f"    - 유형: 국가 정보, 국가: {doc.get('country_name_kor')}")
                elif doc.get('procedure_type'):
                    print(f"    - 유형: 공항 절차, 절차 유형: {doc.get('procedure_type')}, 내용: {doc.get('description', '')[:50]}...")
        else:
            print(f"\n❌ 최종 검색 결과가 없습니다 (쿼리: '{query}').")