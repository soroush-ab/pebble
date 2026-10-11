"""Full Pebble twin in ROS 2: robot_state_publisher + MCU node + RViz.

    ros2 launch pebble_twin twin.launch.py
    ros2 launch pebble_twin twin.launch.py rviz:=false serve_web:=false

The MCU node also serves the original viewer on http://localhost:8730/ and the
/brain WebSocket, so examples/brain_stub.py drives this unchanged.
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    desc = get_package_share_directory('pebble_description')
    urdf = os.path.join(desc, 'urdf', 'pebble.urdf')
    with open(urdf) as fh:
        robot_desc = fh.read()

    rviz = LaunchConfiguration('rviz')
    serve_web = LaunchConfiguration('serve_web')
    web_port = LaunchConfiguration('web_port')

    return LaunchDescription([
        DeclareLaunchArgument('rviz', default_value='true'),
        DeclareLaunchArgument('serve_web', default_value='true'),
        DeclareLaunchArgument('web_port', default_value='8730'),

        Node(package='robot_state_publisher', executable='robot_state_publisher',
             name='robot_state_publisher', output='screen',
             parameters=[{'robot_description': robot_desc}]),

        Node(package='pebble_twin', executable='mcu_node',
             name='pebble_mcu', output='screen',
             parameters=[{'serve_web': serve_web, 'web_port': web_port}]),

        Node(package='rviz2', executable='rviz2', name='rviz2',
             output='screen', condition=IfCondition(rviz),
             arguments=['-d', os.path.join(desc, 'rviz', 'pebble.rviz')]),
    ])
