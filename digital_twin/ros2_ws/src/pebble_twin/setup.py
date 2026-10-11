import os
from glob import glob

from setuptools import setup

package_name = 'pebble_twin'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages',
         ['resource/' + package_name]),
        (os.path.join('share', package_name), ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='COMPSYS 730 Group 5',
    maintainer_email='sabd821@aucklanduni.ac.nz',
    description='Pebble MCU emulator as a ROS 2 node.',
    license='MIT',
    entry_points={
        'console_scripts': [
            'mcu_node = pebble_twin.mcu_node:main',
        ],
    },
)
