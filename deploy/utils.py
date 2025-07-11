import ast
import copy
import os
import traceback
from pathlib import Path

from flask import jsonify


def split_dot_keys(d: dict) -> dict:
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            value = split_dot_keys(value)

        if "." in key:
            parts = key.split(".")
            current = result
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = value
        else:
            result[key] = value
    return result


def recursive_update(yaml_obj, new_values):
    for key, value in new_values.items():
        if key in yaml_obj:
            if isinstance(value, dict) and isinstance(yaml_obj[key], dict):
                recursive_update(yaml_obj[key], value)
            elif isinstance(value, list) and isinstance(yaml_obj[key], list):
                yaml_obj[key] = copy.deepcopy(value)
            else:
                yaml_obj[key] = value
    return yaml_obj


def validate_collaborator_config(master, slaver):
    if not master or not slaver:
        return False, "Missing 'collaborator' configuration"

    required_keys = {"host", "port", "password", "db"}
    if not required_keys.issubset(master.keys()) or not required_keys.issubset(
        slaver.keys()
    ):
        return False, "Incomplete collaborator configuration fields"

    if (master["host"], master["port"]) != (slaver["host"], slaver["port"]):
        return False, "Master and Slaver collaborator config mismatch (HOST or PORT)"

    return True, ""


async def handle_local_tools(slaver_config_path: str, relative_path: str):
    try:
        base_dir = Path(slaver_config_path).parent
        tool_path = base_dir / relative_path / "skill.py"

        if not tool_path.exists():
            return (
                jsonify(
                    {
                        "success": False,
                        "message": f"{tool_path} does not exist",
                        "data": [],
                    }
                ),
                400,
            )

        with open(tool_path, "r", encoding="utf-8") as f:
            source = f.read()

        results = extract_tools_from_ast(source, str(tool_path))
        return jsonify({"success": True, "data": results}), 200

    except Exception as e:
        print(traceback.print_exc())
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"Failed to parse skill.py: {str(e)}",
                    "data": [],
                }
            ),
            400,
        )


async def handle_remote_tools(url):
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(f"{url}/mcp") as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.list_tools()

                tools = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": extract_params(tool.inputSchema),
                    }
                    for tool in response.tools
                ]

        return jsonify({"success": True, "data": tools}), 200

    except Exception as e:
        print(traceback.print_exc())
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"Failed to load remote tools: {str(e)}",
                    "data": [],
                }
            ),
            400,
        )


def extract_tools_from_ast(source: str, filename: str):
    tree = ast.parse(source, filename=filename)
    tools = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.decorator_list:
                continue

            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and getattr(deco.func, "attr", None) == "tool"
                ):
                    parameters = []
                    total_args = len(node.args.args)
                    defaults = [None] * (
                        total_args - len(node.args.defaults)
                    ) + node.args.defaults

                    for arg, default in zip(node.args.args, defaults):
                        if arg.arg == "self":
                            continue
                        arg_type = (
                            ast.unparse(arg.annotation) if arg.annotation else "Any"
                        )
                        default_val = ast.unparse(default) if default else None

                        parameters.append(
                            {
                                "name": arg.arg,
                                "type": arg_type,
                                "default": default_val,
                            }
                        )

                    tools.append(
                        {
                            "name": node.name,
                            "description": ast.get_docstring(node) or "",
                            "parameters": parameters,
                        }
                    )
    return tools


def extract_params(schema):
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    result = []
    for name, prop in properties.items():
        t = prop.get("type", "any")
        if t == "string":
            t = "str"
        elif t == "integer":
            t = "int"
        elif t == "boolean":
            t = "bool"
        else:
            t = t

        default_val = None if name not in required else "required"

        if default_val == "required":
            default_val = None

        result.append({"name": name, "type": t, "default": default_val})
    return result
