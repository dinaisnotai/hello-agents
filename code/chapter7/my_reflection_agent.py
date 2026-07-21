
import re
import json
from typing import Optional, Dict, List
from hello_agents import HelloAgentsLLM, Config, Message
from hello_agents.core.agent import Agent
DEFAULT_PROMPTS = {
    "initial": """请完成任务：{task}""",
    "reflect": """你是一名严格但务实的审查员。请审查以下回答。：
    任务：{task}
    当前回答：{content}

    请仔细审查以下回答，并找出可能的问题或改进空间，并给任务的回复进行评分（满分100分），并给出完整的评分理由和依据。
    评分回复格式为：
    ```python
        {{
            "score": 100,
            "reason": "详细说明评分理由和依据"
        }}
    ```
    """,
        "refine": """请根据反馈改进回答：
    任务：{task}
    上一版：{last_attempt}
    反馈：{feedback}"""
}
def parse_score(response_text: str) -> int:
    # 从模型回复中提取 {...}
    match = re.search(r"\{.*?\}", response_text, re.DOTALL)

    if not match:
        raise ValueError(f"评分结果中没有找到 JSON：{response_text}")

    data = json.loads(match.group())
    score = int(data["score"])
    reason= data["reason"]

    if not 0 <= score <= 100:
        raise ValueError(f"评分超出范围：{score}")

    return score,reason
class MyReflectionAgent(Agent):
    """
    重写的Reflection Agent - 推理与行动结合的智能体
    """

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        max_iterations: int = 5,
        custom_prompts: Optional[Dict[str, str]] = None
    ):
        super().__init__(name, llm, system_prompt, config)
        self.max_iterations = max_iterations
        self.current_history: List[str] = []
        self.prompt_template = custom_prompts if custom_prompts else DEFAULT_PROMPTS
        print(f"✅ {name} 初始化完成，最大步数: {max_iterations}")






    def run(self, input_text: str, **kwargs) -> str:
        """运行Reflection Agent"""
        self.current_history = []
        current_step = 0

        print(f"\n🤖 {self.name} 开始处理问题: {input_text}")

        initial_prompt = self.prompt_template["initial"].format(task=input_text)
        result = self.llm.invoke([{"role": "user", "content": initial_prompt}], **kwargs)
        self.current_history.append(f"Initial result: {result}")

        print(f"\n--- 初稿 ---")
        print(f"Initial attempt: {result}")

        while current_step < self.max_iterations :
            current_step += 1
            print(f"\n--- 第 {current_step} 轮反思 ---")

            prompt = self.prompt_template["reflect"].format(
                task=input_text,
                content=result
            )

            # 2. 调用LLM
            messages = [{"role": "user", "content": prompt}]
            response_text = self.llm.invoke(messages, **kwargs)
            self.current_history.append(f"Reflection: {response_text}")
            

            score,reason=parse_score(response_text)

            print(f"评分: {score}, 评分理由: {reason}")

            if "无需改进" in response_text or score >= 90:
                print("无需改进，结束反思。")
                break

            # 3. 根据反馈改进回答
            refine_prompt = self.prompt_template["refine"].format(
                task=input_text,
                last_attempt=result,
                feedback=response_text
            )
            messages = [{"role": "user", "content": refine_prompt}]
            result = self.llm.invoke(messages, **kwargs)
            self.current_history.append(f"Refined attempt: {result}")
            print(f"改进后的回答: {result}")

        # 如果在循环中没有提前结束，说明达到了最大步数仍未完成任务
        final_answer = result
        self.add_message(Message(input_text, "user"))
        self.add_message(Message(final_answer, "assistant"))
        return final_answer

