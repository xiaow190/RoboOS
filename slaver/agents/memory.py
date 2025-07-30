import json
import traceback
from typing import Dict


def add_object(collaborator, target: str):
    """
    Add an object from the robot's hand back into the environment (at the current position).

    This simulates placing an object (e.g., placing an apple into a basket or onto a table).

    Steps:
    1. Remove the object from robot's hand ("holding").
    2. Add the object to the current location's 'contains' list.
    3. Update the object's own location metadata.
    """
    # Get robot state
    robot_info = collaborator.get_scene_item("robot_info")
    if not robot_info:
        print("[Error] robot_info not found")
        return

    position = robot_info.get("position")
    holding = robot_info.get("holding")

    if holding != target:
        print(f"[Warning] Robot is not holding '{target}', but holding '{holding}'")
        return

    # Add object to the current position
    scene_obj = collaborator.get_scene_item(position)
    if not scene_obj:
        print(f"[Error] Scene object at position '{position}' not found")
        return

    contains = scene_obj.get("contains", [])
    if target not in contains:
        contains.append(target)
    scene_obj["contains"] = contains

    # Update robot's hand (empty it)
    robot_info["holding"] = ""

    # Persist updates to Redis
    collaborator.update_scene_item("robot_info", robot_info)
    collaborator.update_scene_item(position, scene_obj)


def remove_object(collaborator, target: str):
    """
    Remove an object from the current scene location and place it into the robot's hand.

    This simulates the robot grasping or picking up an object.

    Steps:
    1. Remove the target object from the 'contains' list of the current location.
    2. Set the robot's 'holding' field to the target.
    3. Update the target object's 'location' to 'robot_hand'.
    4. Persist all updates to Redis.

    Args:
        target (str): The name of the object to be picked up.
    """
    # Get robot info
    robot_info = collaborator.get_scene_item("robot_info")
    if not robot_info:
        print("[Error] robot_info not found")
        return

    position = robot_info.get("position")

    # Get scene object at current robot position
    scene_obj = collaborator.get_scene_item(position)
    if not scene_obj:
        print(f"[Error] Scene object at position '{position}' not found")
        return

    contains = scene_obj.get("contains", [])
    if target not in contains:
        print(f"[Warning] Object '{target}' not found in '{position}'")
        return

    # Remove object from current container
    contains.remove(target)
    scene_obj["contains"] = contains

    # Update robot's hand (empty it)
    robot_info["holding"] = target

    # Persist updates to Redis
    collaborator.update_scene_item("robot_info", robot_info)
    collaborator.update_scene_item(position, scene_obj)


def position(collaborator, target: str):
    """
    Update the robot's position in the scene.

    This simulates a navigation action: the robot moves itself to a new location.
    The environment is not changed; no object is added, removed, or moved.

    Args:
        target (str): The name of the location to move the robot to.
    """
    # Get current robot info
    robot_info = collaborator.get_scene_item("robot_info")
    if not robot_info:
        print("[Error] robot_info not found")
        return

    # Update position
    robot_info["position"] = target
    success = collaborator.update_scene_item("robot_info", robot_info)
    if not success:
        print(f"[Error] Failed to update robot position to '{target}'")


def get_action_type_prompt(memory_input: Dict) -> str:
    return f"""
You are a robot task planner responsible for updating a symbolic scene memory.

Each tool the robot calls has a side effect on the world, which can be one of the following **scene-level action types**:

- `add_object`: An object that was previously not in the environment (e.g., held by the robot) is placed back into the environment, like placing an apple into a basket.
- `remove_object`: An object is taken out of the environment (e.g., from a table) and held by the robot, such as grasping or picking up something.
- `position`: The environment is not changed; the robot itself may move (e.g., navigation), but no object is added, removed, or moved.

---

Given the following tool execution, predict what scene-level action type this tool represents.

Tool name: {memory_input['tool_name']}
Arguments: {json.dumps(memory_input['arguments'], ensure_ascii=False)}
Result: {memory_input['result']}

---

Answer strictly with one of the following:
[add_object, remove_object, position]

Answer with only one action type from the list above. Do not include any explanation.
"""


def scene_update_from_action(collaborator, action_type: str, args: dict) -> None:
    """
    Apply scene update based on the predicted action type and arguments.

    Args:
        action_type (str): One of ["add_object", "remove_object", "position"]
        args (dict): Arguments required by the action function. For example:
            - {"target": "apple"}
            - {"target": "kitchenTable"}  # for position
    """

    print(f"[Scene Update] Applying `{action_type}` with args {args}")
    try:
        if "remove_object" in action_type:
            target = args.get("object")
            if target:
                remove_object(collaborator, target)
            else:
                print("[Scene Update] Missing `target` for remove_object")

        elif "add_object" in action_type:
            target = args.get("object")
            if target:
                add_object(collaborator, target)
            else:
                print("[Scene Update] Missing `target` for add_object")

        elif "position" in action_type:
            target = args.get("target")
            if target:
                position(collaborator, target)
            else:
                print("[Scene Update] Missing `target` for position")
        else:
            print(f"[Scene Update] No update required for action `{action_type}`")
    except Exception as e:
        print(traceback.print_exc())
        print(f"[Scene Update] Error applying action `{action_type}`: {e}")
