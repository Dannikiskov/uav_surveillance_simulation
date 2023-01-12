#! /usr/bin/env python3

import rospy, socket, threading, ast, cv2, select
from geometry_msgs.msg import PoseStamped
from mavros_msgs.msg import State
from mavros_msgs.srv import CommandBool, CommandBoolRequest, SetMode, SetModeRequest
from sensor_msgs.msg import Image, NavSatFix
from cv_bridge import CvBridge
from people_detector import PeopleDetector


class Drone:
    def __init__(self):

        self.people_detector = PeopleDetector()

        self.current_state = State()

        self.gps_sub = rospy.Subscriber('mavros/global_position/global', NavSatFix, self.gps_cb)

        self.local_pos_sub = rospy.Subscriber('mavros/local_position/pose',
                                              PoseStamped,
                                              self.local_position_cb)


        rospy.init_node('offb_node_py')

        self.state_sub = rospy.Subscriber('mavros/state', State, callback=self.state_cb)

        self.people_detector_activated = False
        self.image_sub = rospy.Subscriber('iris/camera_link/down_raw_image', Image, self.image_cb)

        self.local_pos_pub = rospy.Publisher('mavros/setpoint_position/local', PoseStamped, queue_size=10)

        rospy.wait_for_service('/mavros/cmd/arming')
        self.arming_client = rospy.ServiceProxy('mavros/cmd/arming', CommandBool)

        rospy.wait_for_service('/mavros/set_mode')
        self.set_mode_client = rospy.ServiceProxy('mavros/set_mode', SetMode)

        # Setpoint publishing MUST be faster than 2Hz
        self.rate = rospy.Rate(20)

        # Wait for Flight Controller connection
        while not rospy.is_shutdown() and not self.current_state.connected:
            self.rate.sleep()

        self.offb_set_mode = SetModeRequest()

        self.arm_cmd = CommandBoolRequest()

        self.HOME_COORDINATES = [0, 0, 10]
        self.HOME_COORDINATES_GPS = [55.41545, 10.37111]

        HOST = '127.0.1.1'
        PORT = 8888
        self.client = NetworkClient(HOST, PORT)

    def state_cb(self, msg):
        self.current_state = msg

    def image_cb(self, image):
        if self.people_detector_activated:
            bridge = CvBridge()
            cv_image = bridge.imgmsg_to_cv2(image, 'bgr8')
            boundingboxes = self.people_detector.detect(cv_image)
            self.client.outbox.append(str([self.gps_location.latitude, self.gps_location.longitude]) + ', ' + str(boundingboxes))

        #cv2.imwrite('image.jpg', cv_image)

    def gps_cb(self, gps_location):
        self.gps_location = gps_location

    def local_position_cb(self, data):
        self.local_position = data

    # Returns True if the drone is at the given location, within a threshold
    def at_location(self, location, threshold=1):
        return location[0] - threshold <= self.local_position.pose.position.x <= location[0] + threshold and \
               location[1] - threshold <= self.local_position.pose.position.y <= location[1] + threshold and \
               location[2] - threshold <= self.local_position.pose.position.z <= location[2] + threshold

    def start(self):
        self.client.start()

        pose = PoseStamped()
        pose.pose.position.x = self.HOME_COORDINATES[0]
        pose.pose.position.y = self.HOME_COORDINATES[1]
        pose.pose.position.z = self.HOME_COORDINATES[2]

        # Send a few setpoints before starting
        for i in range(100):
            if rospy.is_shutdown():
                break
            self.local_pos_pub.publish(pose)
            self.rate.sleep()

        self.offb_set_mode.custom_mode = 'OFFBOARD'

        self.arm_cmd.value = True

        last_req = rospy.Time.now()

        waypoints = []

        while not rospy.is_shutdown():
            if self.current_state.mode != 'OFFBOARD' and (rospy.Time.now() - last_req) > rospy.Duration(5.0):
                if self.set_mode_client.call(self.offb_set_mode).mode_sent:
                    rospy.loginfo('OFFBOARD enabled')
                last_req = rospy.Time.now()
            elif not self.current_state.armed and (rospy.Time.now() - last_req) > rospy.Duration(5.0):
                if self.arming_client.call(self.arm_cmd).success:
                    rospy.loginfo('Vehicle armed')
                last_req = rospy.Time.now()

            if self.client.inbox:
                for _ in range(len(self.client.inbox)):
                    message = self.client.inbox.pop(0)
                    message_split = message.split('=')
                    if message_split[0] == 'route':
                        route = ast.literal_eval(message_split[1])
                        route = [[(lat - self.HOME_COORDINATES_GPS[0]) * 111132, (lon - self.HOME_COORDINATES_GPS[1]) * 111415, self.HOME_COORDINATES[2]] for [lat, lon] in route]
                        #route = [[lat, lon, self.HOME_COORDINATES[2]] for [lat, lon] in route]
                        waypoints += route

            if waypoints:
                pose.pose.position.x = waypoints[0][0]
                pose.pose.position.y = waypoints[0][1]
                if (rospy.Time.now() - last_req) > rospy.Duration(5.0) and self.at_location(waypoints[0]):
                    if not self.people_detector_activated:
                        self.people_detector_activated = True
                    waypoint = waypoints.pop(0)
                    msg = 'Waypoint ' + str(waypoint) + ' reached'
                    self.client.outbox.append(msg)
                    rospy.loginfo(msg)
                if not waypoints:
                    self.people_detector_activated = False
                    pose.pose.position.x = self.HOME_COORDINATES[0]
                    pose.pose.position.y = self.HOME_COORDINATES[1]
                    pose.pose.position.z = self.HOME_COORDINATES[2]

            self.local_pos_pub.publish(pose)
            self.rate.sleep()

        # disconnect from server
        self.client.close()


class Receiver(threading.Thread):
    def __init__(self, network):
        threading.Thread.__init__(self)
        self.network = network

    def run(self):
        while True:
            ready = select.select([self.network.client], [], [])
            if ready[0]:
                message = self.network.client.recv(1024)
                self.network.inbox.append(str(message.decode()))


class Sender(threading.Thread):
    def __init__(self, network):
        threading.Thread.__init__(self)
        self.network = network

    def run(self):
        while True:
            if self.network.outbox:
                self.network.client.send(self.network.outbox.pop(0).encode())


class NetworkClient:

    def __init__(self, host, port):

        self.inbox = []
        self.outbox = []

        self.host = host
        self.port = port
        # Create a client socket
        self.client = socket.socket(socket.AF_INET,
                                    socket.SOCK_STREAM)
        # Connect it to the server
        connected = False
        while not connected:
            try:
                self.client.connect((host, port))
                connected = True
            except Exception as e:
                pass

        self.sender = Sender(self)
        self.receiver = Receiver(self)

    def start(self):
        self.sender.start()
        self.receiver.start()

    # Close connection
    def close(self):
        self.sender.join()
        self.receiver.join()
        self.client.close()


def main():
    drone = Drone()
    drone.start()


if __name__ == '__main__':
    main()

