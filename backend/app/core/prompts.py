class Prompt:
    def __init__(self, system_prompt: str, user_prompt: str):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt

    def get_prompt(self, prompt_struct: str, context: str, question: str) -> str:
        formatted_user_prompt = self.user_prompt.format(CONTEXT=context, QUESTION=question)
        return prompt_struct.format(SYSTEM_PROMPT=self.system_prompt, USER_PROMPT=formatted_user_prompt)


SYSTEM_PROMPT = """You are a helpful, respectful and honest assistant.
Your answers should not include any harmful, unethical, racist, sexist, toxic, dangerous, or illegal content.
Please ensure that your responses are socially unbiased and positive in nature.
If you don't know the answer to a question, please don't share false information."""

USER_PROMPT = """Read the context and answer the question.
If it cannot be answered, only say: 'Unanswerable'.
Answer should be concise and professional.
Make sure response is not cut off, and do not give an empty response.
Guidelines for Answering:
1. Understand the Context
2. Base answers solely on the information within the given context; do not rely on external knowledge.
3. Craft responses in full sentences to enhance clarity.
4. Be Concise and Relevant. Avoid unnecessary elaboration.
5. Provide answers without personal opinions or interpretations.
6. Keep your response format consistent, adapting it to fit the nature of the question
7. Rely solely on the provided context. Do not introduce external information.
8. Only Respond in the language of the question. Ensure that the answer is provided in the same language as the question, unless otherwise specified. So therefore, if a question is given in Spanish, you have to answer in Spanish
#### START CONTEXT
Context:
{CONTEXT}
#### END CONTEXT
Question:
{QUESTION}
Answer: """

prompt_struct = """
<|begin_of_text|><|start_header_id|>system<|end_header_id|>
{SYSTEM_PROMPT}<|eot_id|><|start_header_id|>user<|end_header_id|>
{USER_PROMPT}<|eot_id|><|start_header_id|>assistant<|end_header_id|>
"""
