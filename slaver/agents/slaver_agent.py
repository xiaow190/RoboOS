#!/usr/bin/env python
# coding=utf-8
import importlib
import importlib.resources
import inspect
import json
import re
import time
from collections import deque
from logging import getLogger
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
    TypedDict,
    Union,
)

import yaml
from jinja2 import StrictUndefined, Template
from rich.panel import Panel
from rich.text import Text

from tools.agent_types import AgentAudio, AgentImage, handle_agent_output_types
from tools.memory import (
    ActionStep,
    AgentMemory,
    PlanningStep,
    SystemPromptStep,
    TaskStep,
    ToolCall,
)


from agents.models import (
    ChatMessage,
    MessageRole,
)
from tools.monitoring import (
    YELLOW_HEX,
    AgentLogger,
    LogLevel,
    Monitor,
)
from tools.utils import (
    AgentError,
    AgentGenerationError,
    AgentMaxStepsError
)

logger = getLogger(__name__)

def populate_template(template: str, variables: Dict[str, Any]) -> str:
    compiled_template = Template(template, undefined=StrictUndefined)
    try:
        return compiled_template.render(**variables)
    except Exception as e:
        raise Exception(
            f"Error during jinja template rendering: {type(e).__name__}: {e}"
        )


class PlanningPromptTemplate(TypedDict):
    """
    Prompt templates for the planning step.

    Args:
        initial_facts (`str`): Initial facts prompt.
        initial_plan (`str`): Initial plan prompt.
        update_facts_pre_messages (`str`): Update facts pre-messages prompt.
        update_facts_post_messages (`str`): Update facts post-messages prompt.
        update_plan_pre_messages (`str`): Update plan pre-messages prompt.
        update_plan_post_messages (`str`): Update plan post-messages prompt.
    """

    initial_facts: str
    initial_plan: str
    update_facts_pre_messages: str
    update_facts_post_messages: str
    update_plan_pre_messages: str
    update_plan_post_messages: str


class ManagedAgentPromptTemplate(TypedDict):
    """
    Prompt templates for the managed agent.

    Args:
        task (`str`): Task prompt.
        report (`str`): Report prompt.
    """

    task: str
    report: str


class FinalAnswerPromptTemplate(TypedDict):
    """
    Prompt templates for the final answer.

    Args:
        pre_messages (`str`): Pre-messages prompt.
        post_messages (`str`): Post-messages prompt.
    """

    pre_messages: str
    post_messages: str


class PromptTemplates(TypedDict):
    """
    Prompt templates for the agent.

    Args:
        system_prompt (`str`): System prompt.
        planning ([`~agents.PlanningPromptTemplate`]): Planning prompt templates.
        managed_agent ([`~agents.ManagedAgentPromptTemplate`]): Managed agent prompt templates.
        final_answer ([`~agents.FinalAnswerPromptTemplate`]): Final answer prompt templates.
    """

    system_prompt: str
    planning: PlanningPromptTemplate
    managed_agent: ManagedAgentPromptTemplate
    final_answer: FinalAnswerPromptTemplate


EMPTY_PROMPT_TEMPLATES = PromptTemplates(
    system_prompt="",
    planning=PlanningPromptTemplate(
        initial_facts="",
        initial_plan="",
        update_facts_pre_messages="",
        update_facts_post_messages="",
        update_plan_pre_messages="",
        update_plan_post_messages="",
    ),
    managed_agent=ManagedAgentPromptTemplate(task="", report=""),
    final_answer=FinalAnswerPromptTemplate(pre_messages="", post_messages=""),
)

class MultiStepAgent:
    """
    Agent class that solves the given task step by step, using the ReAct framework:
    While the objective is not reached, the agent will perform a cycle of action (given by the LLM) and observation (obtained from the environment).

    Args:
        tools (`list[Tool]`): [`Tool`]s that the agent can use.
        model (`Callable[[list[dict[str, str]]], ChatMessage]`): Model that will generate the agent's actions.
        prompt_templates ([`~agents.PromptTemplates`], *optional*): Prompt templates.
        max_steps (`int`, default `20`): Maximum number of steps the agent can take to solve the task.
        verbosity_level (`LogLevel`, default `LogLevel.INFO`): Level of verbosity of the agent's logs.
        step_callbacks (`list[Callable]`, *optional*): Callbacks that will be called at each step.
    """
    def __init__(
        self,
        tools: List[Dict[str, str]],
        tools_path: str,
        model: Callable[[List[Dict[str, str]]], ChatMessage],
        prompt_templates: Optional[PromptTemplates] = None,
        max_steps: int = 20,
        verbosity_level: LogLevel = LogLevel.INFO,
        step_callbacks: Optional[List[Callable]] = None,
        log_file: Optional[str] = None,
    ):
        self.tools = tools
        self.tools_path = tools_path
        self.model = model
        self.prompt_templates = prompt_templates or EMPTY_PROMPT_TEMPLATES
        self.max_steps = max_steps
        self.step_number = 0
        self.state = {}
        self.system_prompt = self.initialize_system_prompt()
        self.memory = AgentMemory(self.system_prompt)
        self.logger = AgentLogger(level=verbosity_level, log_file=log_file)
        self.monitor = Monitor(self.model, self.logger)
        self.step_callbacks = step_callbacks if step_callbacks is not None else []
        self.step_callbacks.append(self.monitor.update_metrics)  
        
    def initialize_system_prompt(self):
        """To be implemented in child classes"""
        raise NotImplementedError
    
    def run(
        self,
        task: str,
        stream: bool = False,
        reset: bool = True,
        images: Optional[List[str]] = None,
        additional_args: Optional[Dict] = None,
        max_steps: Optional[int] = None,
    ):
        """
        Run the agent for the given task.

        Args:
            task (`str`): Task to perform.
            stream (`bool`): Whether to run in a streaming way.
            reset (`bool`): Whether to reset the conversation or keep it going from previous run.
            images (`list[str]`, *optional*): Paths to image(s).
            additional_args (`dict`, *optional*): Any other variables that you want to pass to the agent run, for instance images or dataframes. Give them clear names!
            max_steps (`int`, *optional*): Maximum number of steps the agent can take to solve the task. if not provided, will use the agent's default value.

        Example:
        ```py
        from smolagents import CodeAgent
        agent = CodeAgent(tools=[])
        agent.run("What is the result of 2 power 3.7384?")
        ```
        """
        max_steps = max_steps or  self.max_steps
        self.task = task
        if additional_args:
            self.state.update(additional_args)
            self.task += f"""
You have been provided with these additional arguments, that you can access using the keys as variables in your python code:
{str(additional_args)}."""

        if reset:
            self.memory.reset()
            self.step_number = 1


        self.logger.log_task(
            content=self.task.strip(),
            subtitle=f"{type(self.model).__name__} - {(self.model.model_id if hasattr(self.model, 'model_id') else '')}",
            level=LogLevel.INFO,
            title=self.name if hasattr(self, "name") else None,
        )
        self.memory.steps.append(TaskStep(task=self.task, task_images=images))
        if stream:
            return self._run_stream(task=self.task, max_steps=max_steps, images=images)
        return deque(
            self._run_stream(task=self.task, max_steps=max_steps, images=images), maxlen=1
        )[0]
        
    def _run_stream(self, task: str, max_steps: int, images: Optional[List[str]] = None)  -> Generator[ActionStep, None, None]:
        final_answer = None
        while not final_answer and self.step_number <= max_steps:
            step_start_time = time.time()
            step = ActionStep(
                step_number=self.step_number,
                start_time=step_start_time,
                observations_images=images,
            )
            try:
                final_answer = self._execute_step(task, step)

            except AgentError as e:
                step.error = e
            finally:
                step.end_time = time.time()
                self.memory.steps.append(step)
                yield step
                self.step_number += 1
        
        if not final_answer:
            final_answer = self._handle_max_steps_reached(task, images, step_start_time)
            yield final_answer
        yield handle_agent_output_types(final_answer)
                
    def _handle_max_steps_reached(
        self, task: str, images: List[str], step_start_time: float
    ) -> Any:
        final_answer = self.provide_final_answer(task, images)
        final_memory_step = ActionStep(
            step_number=self.step_number,
            error=AgentMaxStepsError("Reached max steps.", self.logger),
        )
        final_memory_step.action_output = final_answer
        final_memory_step.end_time = time.time()
        final_memory_step.duration = final_memory_step.end_time - step_start_time
        self.memory.steps.append(final_memory_step)
        for callback in self.step_callbacks:
            callback(final_memory_step) if len(
                inspect.signature(callback).parameters
            ) == 1 else callback(final_memory_step, agent=self)
        return final_answer
    
    def provide_final_answer(self, task: str, images: Optional[list[str]]) -> str:
        """
        Provide the final answer to the task, based on the logs of the agent's interactions.

        Args:
            task (`str`): Task to perform.
            images (`list[str]`, *optional*): Paths to image(s).

        Returns:
            `str`: Final answer to the task.
        """
        messages = []
        if images:
            messages[0]["content"].append({"type": "image"})
        messages += self.write_memory_to_messages()[1:]
        messages += [
            {
                "role": MessageRole.USER,
                "content": [
                    {
                        "type": "text",
                        "text": populate_template(
                            self.prompt_templates["final_answer"]["post_messages"],
                            variables={"task": task},
                        ),
                    }
                ],
            }
        ]
        try:
            chat_message: ChatMessage = self.model(messages)
            return chat_message.content
        except Exception as e:
            return f"Error in generating final LLM output:\n{e}"
    
    def _execute_step(self, task: str, step: ActionStep) -> Union[None, Any]:
        
        self.logger.log_rule(f"Step {self.step_number}", level=LogLevel.INFO)
        return self.step(step)

    def step(self, memory_step: ActionStep) -> Optional[Any]:
        """To be implemented in children classes. Should return either None if the step is not final."""
        raise NotImplementedError
    
    def execute_tool_call(
        self, tool_name: str, arguments: Union[Dict[str, str], str]
    ) -> Any:
        import importlib.util
        from pathlib import Path
        
        file_path = Path(self.tools_path).absolute()
        module_name = file_path.stem
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        func = getattr(module, tool_name)
        result = func(**arguments)
            
        return result
    
    def write_memory_to_messages(
        self,
        summary_mode: Optional[bool] = False,
    ) -> List[Dict[str, str]]:
        """
        Reads past llm_outputs, actions, and observations or errors from the memory into a series of messages
        that can be used as input to the LLM. Adds a number of keywords (such as PLAN, error, etc) to help
        the LLM.
        """
        messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
        for memory_step in self.memory.steps:
            messages.extend(memory_step.to_messages(summary_mode=summary_mode))
        return messages


class ToolCallingAgent(MultiStepAgent):
    """
    This agent uses JSON-like tool calls, using method `model.get_tool_call` to leverage the LLM engine's tool calling capabilities.

    Args:
        tools (`list[Tool]`): [`Tool`]s that the agent can use.
        model (`Callable[[list[dict[str, str]]], ChatMessage]`): Model that will generate the agent's actions.
        prompt_templates ([`~agents.PromptTemplates`], *optional*): Prompt templates.
        planning_interval (`int`, *optional*): Interval at which the agent will run a planning step.
        **kwargs: Additional keyword arguments.
    """

    def __init__(
        self,
        tools: List[Dict[str, str]],
        tools_path: str,
        model: Callable[[List[Dict[str, str]]], ChatMessage],
        prompt_templates: Optional[PromptTemplates] = None,
        # planning_interval: Optional[int] = None,
        communicator=None,
        robot_name: str = None,
        **kwargs,
    ):
        prompt_templates = prompt_templates or yaml.safe_load(importlib.resources.files("prompts").joinpath("toolcalling_agent.yaml").read_text())
        self.tool_call = []
        self.communicator = communicator
        self.robot_name = robot_name
        super().__init__(
            tools=tools,
            tools_path=tools_path,
            model=model,
            prompt_templates=prompt_templates,
            # planning_interval=planning_interval,
            **kwargs,
        )
        
    def initialize_system_prompt(self) -> str:
        
        system_prompt = populate_template(
            self.prompt_templates["system_prompt"],
            variables={"tools": self.tools, "managed_agents": {}},
        )
        return system_prompt

    def _extract_json(self, input_string):
        """Extract JSON from a string."""
        start_marker = "```json"
        end_marker = "```"
        try:
            start_idx = input_string.find(start_marker)
            end_idx = input_string.find(end_marker, start_idx + len(start_marker))
            if start_idx == -1 or end_idx == -1:
                self.logger.log("[WARNING] JSON markers not found in the string.")
                return None
            json_str = input_string[start_idx + len(start_marker) : end_idx].strip()
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            self.logger.log(
                f"[WARNING] JSON cannot be extracted from the string.\n{e}"
            )
            return None

    def _handle_final_answer(self, tool_arguments: str, memory_step: ActionStep) -> Union[str, None]:
        if isinstance(tool_arguments, dict):
            if "answer" in tool_arguments:
                answer = tool_arguments["answer"]
            else:
                answer = tool_arguments
        else:
            answer = tool_arguments
        if (
            isinstance(answer, str) and answer in self.state.keys()
        ):  # if the answer is a state variable, return the value
            final_answer = self.state[answer]
            self.logger.log(
                f"[bold {YELLOW_HEX}]Final answer:[/bold {YELLOW_HEX}] Extracting key '{answer}' from state to return value '{final_answer}'.",
                level=LogLevel.INFO,
            )
        else:
            final_answer = answer
            self.logger.log(
                Text(f"Final answer: {final_answer}", style=f"bold {YELLOW_HEX}"),
                level=LogLevel.INFO,
            )

        memory_step.action_output = final_answer
        return final_answer


    def _handle_tool_call(self, tool_name: str, tool_arguments: dict, memory_step: ActionStep) -> Union[str, None]:
        self.logger.log(
            Panel(
                Text(f"Calling tool: '{tool_name}' with arguments: {tool_arguments}")
            ),
            level=LogLevel.INFO,
        )
        observation = self.execute_tool_call(tool_name, tool_arguments)
        observation_type = type(observation)
        if observation_type in [AgentImage, AgentAudio]:
            observation_name = "image.png" if observation_type == AgentImage else "audio.mp3"
            # TODO: observation naming could allow for different names of same type
            self.state[observation_name] = observation
            updated_information = f"Stored '{observation_name}' in memory."
        else:
            updated_information = str(observation).strip()
            
        self.tool_call.append({"tool_name": tool_name, "result": observation})
        self.logger.log(
            f"Observations: {updated_information.replace('[', '|')}",  # escape potential rich-tag-like components
            level=LogLevel.INFO,
        )
        memory_step.observations = updated_information
        return None


    def step(self, memory_step: ActionStep) -> Union[None, Any]:
        """
        Perform one step in the ReAct framework: the agent thinks, acts, and observes the result.
        Returns None if the step is not final.
        """
        memory_messages = self.write_memory_to_messages()
        self.input_messages = memory_messages

        # Add new step in logs
        memory_step.model_input_messages = memory_messages.copy()
        try:
            model_message: ChatMessage = self.model(
                memory_messages,
                tools=self.tools,
                stop_sequences=["Observation:"],
            )
            memory_step.model_output_message = model_message
        except Exception as e:
            raise AgentGenerationError(
                f"Error in generating tool call with model:\n{e}", self.logger
            ) from e
        self.logger.log_markdown(
            content=model_message.content
            if model_message.content
            else str(model_message.raw),
            title="Output message of the LLM:",
            level=LogLevel.DEBUG,
        )
        if model_message.tool_calls:
            tool_call = model_message.tool_calls[0]
            tool_name, tool_call_id = tool_call.function.name, tool_call.id
            tool_arguments = tool_call.function.arguments
        else:
            final_answer = model_message.content
            self.logger.log(
                Text(f"Final answer: {final_answer}", style=f"bold {YELLOW_HEX}"),
                level=LogLevel.INFO,
            )
            tool_json = self._extract_json(final_answer)
            if tool_json:
                tool_name, tool_arguments = tool_json["name"], tool_json["arguments"]
                tool_call_id = model_message.raw.id 
            else:
                memory_step.action_output = final_answer
                return final_answer
            
        if tool_name == "final_answer":
            return self._handle_final_answer(tool_arguments, memory_step)
            
        memory_step.tool_calls = [
            ToolCall(name=tool_name, arguments=tool_arguments, id=tool_call_id)
        ]
        return self._handle_tool_call(tool_name, tool_arguments, memory_step)

            
