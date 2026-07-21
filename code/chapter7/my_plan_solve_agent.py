# 默认规划器提示词模板
DEFAULT_PLANNER_PROMPT = """
你是一个顶级的AI规划专家。你的任务是将用户提出的复杂问题分解成一个由多个简单步骤组成的行动计划。
请确保计划中的每个步骤都是一个独立的、可执行的子任务，并且严格按照逻辑顺序排列。
你的输出必须是一个Python列表，其中每个元素都是一个描述子任务的字符串。

问题: {question}

请严格按照以下格式输出你的计划:
```python
["步骤1", "步骤2", "步骤3", ...]
```
"""

# 默认执行器提示词模板
DEFAULT_EXECUTOR_PROMPT = """
你是一位顶级的AI执行专家。你的任务是严格按照给定的计划，一步步地解决问题。
你将收到原始问题、完整的计划、以及到目前为止已经完成的步骤和结果。
请你专注于解决"当前步骤"，并仅输出该步骤的最终答案，不要输出任何额外的解释或对话。

# 原始问题:
{question}

# 完整计划:
{plan}

# 历史步骤与结果:
{history}

# 当前步骤:
{current_step}

请仅输出针对"当前步骤"的回答:
"""
import ast
import re
from typing import Optional, Dict, List
from hello_agents import HelloAgentsLLM, Config, Message
from hello_agents.core.agent import Agent

class MyPlanAndSolveAgent(Agent):
    """
    重写的Reflection Agent - 推理与行动结合的智能体
    """

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        max_steps: int = 5,
        custom_prompts: Optional[Dict[str, str]] = None
    ):
        super().__init__(name, llm, system_prompt, config)
        self.max_steps = max_steps
        self.current_history: List[str] = []
        self.prompt_template = custom_prompts if custom_prompts else DEFAULT_EXECUTOR_PROMPT
        print(f"✅ {name} 初始化完成，最大步数: {max_steps}")

    def run(self, input_text: str, **kwargs) -> str:
        """运行PlanAndSolve Agent"""
        self.current_history = []
        current_step = 0

        print(f"\n🤖 {self.name} 开始处理问题: {input_text}")

        planner_prompt = DEFAULT_PLANNER_PROMPT.format(question=input_text)
        plan = self.llm.invoke([{"role": "user", "content": planner_prompt}], **kwargs)
        
        cleaned_plan = re.sub(
            r"^```(?:python)?\s*|\s*```$",
            "",
            plan.strip(),
        )
        self.current_history.append(f"Initial plan: {cleaned_plan}")

# 字符串转成 Python 列表
        plan_steps = ast.literal_eval(cleaned_plan)

        print(f"\n--- 计划 ---")
        print(f"Initial plan: {cleaned_plan}")

        for current_step, step_content in enumerate(
            plan_steps[:self.max_steps],
            start=1
        ):
            
            print(f"\n--- 第 {current_step}  步 ---")

            prompt = self.prompt_template.format(
                question=input_text,
                plan=plan,
                history="\n".join(self.current_history),
                current_step=step_content
            )

            # 2. 调用LLM
            messages = [{"role": "user", "content": prompt}]
            response_text = self.llm.invoke(messages, **kwargs)
            self.current_history.append(f"步骤 {current_step}: {response_text}")
            print(f"步骤 {current_step}: {response_text}")
            current_step += 1



        result= response_text
        # 如果在循环中没有提前结束，说明达到了最大步数仍未完成任务
        final_answer = result
        self.add_message(Message(input_text, "user"))
        self.add_message(Message(final_answer, "assistant"))
        return final_answer

