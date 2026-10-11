"""RViz + sliders only, no twin. Useful for checking the URDF itself.

    ros2 launch pebble_description display.launch.py
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    share = get_package_share_directory('pebble_description')
    urdf = os.path.join(share, 'urdf', 'pebble.urdf')
    with open(urdf) as fh:
        robot_desc = fh.read()

    return LaunchDescription([
        Node(package='robot_state_publisher', executable='robot_state_publisher',
             output='screen', parameters=[{'robot_description': robot_desc}]),
        Node(package='joint_state_publisher_gui',
             executable='joint_state_publisher_gui', output='screen'),
        Node(package='rviz2', executable='rviz2', output='screen',
             arguments=['-d', os.path.join(share, 'rviz', 'pebble.rviz')]),
    ])
