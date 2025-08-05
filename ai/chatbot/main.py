from ai.chatbot.graph.flow import build_chat_graph

chat_graph = build_chat_graph()

result = chat_graph.invoke({
    "user_input": "출국장 A 혼잡도가 어떻게 돼?"
})

print("📌 예측 인텐트:", result.get("intent"))
print("📌 슬롯 정보:")
for word, slot in result.get("slots", []):
    print(f" - {word}: {slot}")

print("💬 응답:", result.get("response"))