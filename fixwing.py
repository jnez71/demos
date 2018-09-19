#!/usr/bin/env python2
"""
Basic fixed-wing airplane simulator to test out an intuitive model.
This was a thrown-together messy modification of the software in
my multicopter repo.

"""
from __future__ import division
from threading import Thread
from collections import deque
import time

import numpy as np; npl = np.linalg  # pip install numpy
from inputs import devices, get_gamepad  # pip install inputs
from mayavi import mlab  # http://docs.enthought.com/mayavi/mayavi/installation.html
from tvtk.tools import visual  # ^^^

###################################################################### ADMIN SETUP

# Redirect pointlessly spammed mayavi warnings
import os, vtk
if os.path.exists("/dev/null"): shadow_realm = "/dev/null"
else: shadow_realm = "c:\\nul"
mlab_warning_output = vtk.vtkFileOutputWindow()
mlab_warning_output.SetFileName(shadow_realm)
vtk.vtkOutputWindow().SetInstance(mlab_warning_output)

###################################################################### MATH HELPERS

def quaternion_inverse(q):
    """
    Returns the inverse of the given quaternion q = [x, y, z, w].

    """
    invq = np.copy(q)
    invq[:3] = -invq[:3]
    return invq

def quaternion_multiply(ql, qr):
    """
    Returns the quaternion multiplication ql * qr all in the form [x, y, z, w].

    """
    return np.array((ql[0]*qr[3] + ql[1]*qr[2] - ql[2]*qr[1] + ql[3]*qr[0],
                    -ql[0]*qr[2] + ql[1]*qr[3] + ql[2]*qr[0] + ql[3]*qr[1],
                     ql[0]*qr[1] - ql[1]*qr[0] + ql[2]*qr[3] + ql[3]*qr[2],
                    -ql[0]*qr[0] - ql[1]*qr[1] - ql[2]*qr[2] + ql[3]*qr[3]))

def rotate_vector(q, v, reverse=False):
    """
    Applies the given quaternion to a vector v.
    If reverse is set to True, the inverse quaternion is applied instead.

    """
    uv = 2*np.cross(q[:-1], v)
    if reverse: return v - q[-1]*uv + np.cross(q[:-1], uv)
    else: return v + q[-1]*uv + np.cross(q[:-1], uv)

def rotvec_from_quaternion(q):
    """
    Returns the rotation vector corresponding to the quaternion q = [x, y, z, w].
    A rotation vector is the product of the angle of rotation (0 to pi) and
    axis of rotation (unit vector) of an SO3 quantity like a quaternion.

    """
    q = np.array(q, dtype=np.float64)
    sina2 = npl.norm(q[:-1])
    if np.isclose(sina2, 0): return np.zeros(3, dtype=np.float64)
    if q[-1] < 0: q = -q
    return 2*np.arccos(q[-1]) * q[:-1]/sina2

def quaternion_from_rotvec(r):
    """
    Returns the quaternion [x, y, z, w] equivalent to the given rotation vector r.

    """
    angle = np.mod(npl.norm(r), 2*np.pi)
    if np.isclose(angle, 0): return np.array([0, 0, 0, 1], dtype=np.float64)
    return np.concatenate((np.sin(angle/2)*np.divide(r, angle), [np.cos(angle/2)]))

def rotmat_from_quaternion(q):
    """
    Returns the rotation matrix associated with the quaternion q = [x, y, z, w].

    """
    Q = 2*np.outer(q, q)
    return np.array([[1-Q[1, 1]-Q[2, 2],   Q[0, 1]-Q[2, 3],   Q[0, 2]+Q[1, 3]],
                     [  Q[0, 1]+Q[2, 3], 1-Q[0, 0]-Q[2, 2],   Q[1, 2]-Q[0, 3]],
                     [  Q[0, 2]-Q[1, 3],   Q[1, 2]+Q[0, 3], 1-Q[0, 0]-Q[1, 1]]], dtype=np.float64)

def euler_from_quaternion(q):
    """
    Returns the (roll, pitch, yaw) in radians associated with the quaternion q = [x, y, z, w].

    """
    return np.array((np.arctan2(2*(q[3]*q[0] + q[1]*q[2]), 1 - 2*(q[0]**2 + q[1]**2)),
                     np.arcsin(2*(q[3]*q[1] - q[2]*q[0])),
                     np.arctan2(2*(q[3]*q[2] + q[0]*q[1]), 1 - 2*(q[1]**2 + q[2]**2))))

def quaternion_from_euler(roll, pitch, yaw):
    """
    Returns the quaternion q = [x, y, z, w] associated with the Euler angles roll, pitch, and yaw in radians.

    """
    cr = np.cos(0.5*roll)
    sr = np.sin(0.5*roll)
    cp = np.cos(0.5*pitch)
    sp = np.sin(0.5*pitch)
    cy = np.cos(0.5*yaw)
    sy = np.sin(0.5*yaw)
    return np.array([cy*sr*cp - sy*cr*sp,
                     cy*cr*sp + sy*sr*cp,
                     sy*cr*cp - cy*sr*sp,
                     cy*cr*cp + sy*sr*sp], dtype=np.float64)

def unwrap_angle(ang):
    """
    Returns an equivalent angle to ang in radians on [-np.pi, np.pi].

    """
    return np.mod(ang + np.pi, 2*np.pi) - np.pi

def ori_error(qdes, q):
    """
    Does a Lie algebraic orientation error computation given quaternions qdes and q as [x, y, z, w].
    The returned 3-vector is a rotvec in body-coordinates pointing along the geodesic from q to qdes.

    """
    return rotvec_from_quaternion(quaternion_multiply(quaternion_inverse(q), qdes))

###################################################################### WORLD SETUP

# Local gravity (world frame)
grav = np.array([0.0, 0.0, -9.81])  # m/s^2

# Local density of air
dens = 1.225  # kg/m^3

# Function for wind velocity at time t
def wind(t):
    return np.array([0, 0, 0])  # m/s

###################################################################### MODEL SETUP

class Surface(object):
    def __init__(self, pc, q0, Cp1, Cp2, Cq, upoint, uaxis, umin, umax, udot):
        self.pc = pc  # center of pressure in surface coords
        self.q0 = q0  # initial orientation of surface relative to body
        self.Cp1 = Cp1
        self.Cp2 = Cp2
        self.Cq = Cq
        self.upoint = upoint  # mounting point in body coords (always surface origin)
        self.uaxis = uaxis / npl.norm(uaxis)  # axis of joint rotation in body coords
        self.umin = umin  # minimum joint angle
        self.umax = umax  # maximum joint angle
        self.udot = abs(udot)  # maximum deflection rate
        self.ucmd = 0  # current joint command
        self.u = 0  # current joint angle
        self.q = np.copy(self.q0)
        self.pc_body = self.upoint + rotate_vector(self.q, self.pc)

    def update(self, dt):
        err = self.ucmd - self.u
        du = dt*self.udot
        if abs(err) < du: self.u = self.ucmd
        elif err > 0: self.u += du
        else: self.u -= du
        self.u = np.clip(self.u, self.umin, self.umax)
        self.q = quaternion_multiply(self.q0, quaternion_from_rotvec(self.u*self.uaxis))
        self.pc_body = self.upoint + rotate_vector(self.q, self.pc)

class FixWing(object):
    def __init__(self):
        # Total mass
        self.m = np.float64(10)  # kg
        self.invm = 1/self.m

        # Inertia matrix
        self.M = np.zeros((3, 3), dtype=np.float64)  # kg*m^2
        self.M[0, 0] = 2*self.m
        self.M[1, 1] = 2*self.m
        self.M[2, 2] = 4*self.m
        self.M[2, 0] = self.M[0, 2] = 1*self.m
        self.M[1, 0] = self.M[0, 1] = 0
        self.M[2, 1] = self.M[1, 2] = 0
        self.invM = npl.inv(self.M)

        # Linear and quadratic translational drag coefficients
        self.Cp1 = 0.01*np.array([1, 10, 150], dtype=np.float64)  # N/(m/s)
        self.Cp2 = 0.01*np.array([1, 10, 150], dtype=np.float64)  # N/(m/s)^2

        # Main body center of drag and rotational drag coefficients
        self.rc = np.array([0, 0, 0], dtype=np.float64)  # m
        self.Cq = np.array([30, 40, 20], dtype=np.float64)  # N/(rad/s)

        # Thrust from throttle ratio
        self.kthr = 100  # N/eff

        # Flight surfaces
        raileron = Surface(pc=np.array([0, -1, 0]),
                           q0=np.array([0, 0, 0, 1]),
                           Cp1=np.array([0, 0, 0.01]),
                           Cp2=np.array([0, 0, 0.01]),
                           Cq=np.array([0, 0, 0]),
                           upoint=np.array([-0.1, -0.05, 0]),
                           uaxis=np.array([0, 1, 0]),
                           umin=-np.pi/6,
                           umax=np.pi/6,
                           udot=np.pi/1)
        laileron = Surface(pc=np.array([0, 1, 0]),
                           q0=np.array([0, 0, 0, 1]),
                           Cp1=np.array([0, 0, 0.005]),
                           Cp2=np.array([0, 0, 0.005]),
                           Cq=np.array([0, 0, 0]),
                           upoint=np.array([-0.1, 0.05, 0]),
                           uaxis=np.array([0, 1, 0]),
                           umin=-np.pi/6,
                           umax=np.pi/6,
                           udot=np.pi/1)
        elevator = Surface(pc=np.array([-1.3, 0, 0]),
                           q0=np.array([0, 0, 0, 1]),
                           Cp1=np.array([0, 0, 0.05]),
                           Cp2=np.array([0, 0, 0.05]),
                           Cq=np.array([0, 0, 0]),
                           upoint=np.array([-1.3, 0, 0.8]),
                           uaxis=np.array([0, -1, 0]),
                           umin=-np.pi/6,
                           umax=np.pi/6,
                           udot=np.pi/1)
        rudder = Surface(pc=np.array([-1.5, 0, 0]),
                         q0=np.array([0, 0, 0, 1]),
                         Cp1=np.array([0, 0.05, 0]),
                         Cp2=np.array([0, 0.05, 0]),
                         Cq=np.array([0, 0, 0]),
                         upoint=np.array([-1, 0, 0]),
                         uaxis=np.array([0, 0, 1]),
                         umin=-np.pi/6,
                         umax=np.pi/6,
                         udot=np.pi/1)
        self.surfaces = [raileron, laileron, elevator, rudder]

        # Initial rigid body state, modified by self.update function
        self.p = np.array([0, -20, 0], dtype=np.float64)  # m
        self.q = quaternion_from_euler(np.deg2rad(0), np.deg2rad(0), np.deg2rad(0))  # quaternion
        self.v = np.array([0, 0, 0], dtype=np.float64)  # m/s
        self.w = np.array([0, 0, 0], dtype=np.float64)  # rad/s

    def update(self, thr, ail, elv, rud, t, dt):
        """
        Updates internal rigid body state given throttle, surface commands,
        the current time, and the timestep to forward simulate.

        """
        wind_body = rotate_vector(self.q, wind(t), reverse=True)
        vair = self.v + np.cross(self.w, self.rc) - wind_body

        F_thr = np.array([self.kthr*thr, 0, 0])
        F_drag = -dens*(self.Cp1 + self.Cp2*np.abs(vair))*vair
        F_grav = self.m * rotate_vector(self.q, grav, reverse=True)
        T_drag = np.cross(self.rc, F_drag) - self.Cq*self.w

        ucmd = [ail*self.surfaces[0].umax, -ail*self.surfaces[1].umax, elv*self.surfaces[2].umax, rud*self.surfaces[3].umax]
        F_surf_net = np.zeros(3)
        T_surf_net = np.zeros(3)
        for i, surf in enumerate(self.surfaces):
            surf.ucmd = ucmd[i]
            surf.update(dt)
            vsurf = rotate_vector(surf.q, self.v + np.cross(self.w, surf.pc_body) - wind_body, reverse=True) # ignores rotation of surface itself
            F_surf = rotate_vector(surf.q, -dens*(surf.Cp1 + surf.Cp2*np.abs(vsurf))*vsurf)
            T_surf = np.cross(surf.pc_body, F_surf) - rotate_vector(surf.q, surf.Cq*rotate_vector(surf.q, self.w, reverse=True))
            F_surf_net = F_surf_net + F_surf
            T_surf_net = T_surf_net + T_surf

        ap = self.invm*(F_thr + F_surf_net + F_drag + F_grav) - np.cross(self.w, self.v)
        aq = self.invM.dot(T_surf_net + T_drag - np.cross(self.w, self.M.dot(self.w)))

        self.p = self.p + rotate_vector(self.q, dt*self.v + 0.5*(dt**2)*ap)
        self.q = quaternion_multiply(self.q, quaternion_from_rotvec(dt*self.w + 0.5*(dt**2)*aq))
        self.v = self.v + dt*ap
        self.w = self.w + dt*aq

        # Basic ground
        if self.p[2] < -0.01:
            self.p[2] = 0
            v_world = rotate_vector(self.q, self.v)
            if v_world[2] < 0:
                v_world[2] = 0
                self.v = rotate_vector(self.q, v_world, reverse=True)

###################################################################### SCENE SETUP

class Viz(object):

    def __init__(self, surfaces):
        self.building_layout = np.ones((5, 5))
        self.building_size = (30, 30, 40)  # m
        self.building_spacing = np.float64(100)  # m
        self.fig = mlab.figure(size=(500, 500), bgcolor=(0.1, 0.1, 0.1))

        # Set figure for visual objects
        visual.set_viewer(self.fig)

        # Convenient local aliases
        nx, ny = self.building_layout.shape
        n = nx * ny

        # Beautiful colors
        self.building_colors = map(tuple, np.array((np.linspace(0.0, 0.0, n),
                                                    np.linspace(0.8, 0.3, n),
                                                    np.linspace(0.3, 0.8, n))).T)

        # For storing buildings and their locations
        self.buildings = []
        self.building_centers = np.zeros((n, 2))

        # Generate buildings
        for i, x in enumerate(np.linspace(0, (nx-1)*(self.building_size[0] + self.building_spacing), nx)):
            for j, y in enumerate(np.linspace(0, (ny-1)*(self.building_size[1] + self.building_spacing), ny)):
                if not self.building_layout[i, j]: continue
                idx = int(ny*i + j)
                self.building_centers[idx] = (x, y)
                self.buildings.append(visual.box(x=x, y=y, z=self.building_size[2]/2, size=self.building_size, color=self.building_colors[idx]))

        # Generate ground plane
        ground_xx, ground_yy = map(np.transpose, np.meshgrid(np.linspace(np.min(self.building_centers[:, 0]-50), np.max(self.building_centers[:, 0]+2500), 40),
                                                                 np.linspace(np.min(self.building_centers[:, 1]-50), np.max(self.building_centers[:, 1]+2500), 40)))
        self.ground = mlab.surf(ground_xx, ground_yy, np.random.sample(np.shape(ground_xx))-0.8, colormap="ocean", warp_scale=1)

        # Generate aircraft
        self.headspan = 0.4+2
        self.tailspan = 0.6+2
        self.wingspan = 0.8*(self.headspan + self.tailspan)
        self.sweep = -0.2*0
        self.rudheight = 0.2*self.wingspan
        self.aircraft_nodes = np.vstack(([(self.headspan, 0, 0), (-self.tailspan, 0, 0)])).T
                                         # [(0, 0, 0), (self.sweep, self.wingspan/2, 0)],
                                         # [(0, 0, 0), (self.sweep, -self.wingspan/2, 0)],
                                         # [(-self.tailspan, 0, 0), (-self.tailspan+self.sweep/5, 0, self.rudheight)],
                                         # [(-self.tailspan+self.sweep/5, 0, self.rudheight), (-self.tailspan+self.sweep/5, self.wingspan/4, self.rudheight)],
                                         # [(-self.tailspan+self.sweep/5, 0, self.rudheight), (-self.tailspan+self.sweep/5, -self.wingspan/4, self.rudheight)])).T
        self.aircraft_fusel = np.vstack(([(self.headspan, 0, 0), (0, 0, 0)],
                                         [(-self.tailspan, 0, 0), (-self.tailspan+self.sweep/5, 0, self.rudheight)])).T
        self.aircraft_wings = np.vstack(([(self.sweep, self.wingspan/2, 0), (self.sweep, -self.wingspan/2, 0)])).T
        self.aircraft_tail = np.vstack(([(-self.tailspan+self.sweep/4, 0.25*self.wingspan, self.rudheight), (-self.tailspan+self.sweep/4, -0.25*self.wingspan, self.rudheight)])).T
        self.aircraft_nodes_plot = mlab.points3d(self.aircraft_nodes[0, :], self.aircraft_nodes[1, :], self.aircraft_nodes[2, :], scale_factor=0.2, color=(0.5, 0.5, 0.5))
        self.aircraft_fusel_plot = mlab.plot3d(self.aircraft_fusel[0, :], self.aircraft_fusel[1, :], self.aircraft_fusel[2, :], tube_sides=10, tube_radius=0.08, color=(1, 0, 0))
        self.aircraft_wings_plot = mlab.plot3d(self.aircraft_wings[0, :], self.aircraft_wings[1, :], self.aircraft_wings[2, :], tube_sides=10, tube_radius=0.08, color=(1, 0, 1))
        self.aircraft_tail_plot = mlab.plot3d(self.aircraft_tail[0, :], self.aircraft_tail[1, :], self.aircraft_tail[2, :], tube_sides=10, tube_radius=0.05, color=(1, 1, 0))
        self.aircraft_surface_plots = []
        self.aircraft_surface_corners = np.array([[ 0.2,  1, 0],
                                                  [-0.2,  1, 0],
                                                  [-0.2, -1, 0],
                                                  [ 0.2, -1, 0]])
        self.rudder_corners = np.array([[ 0.2, 0,  0.7,],
                                        [-0.2, 0,  0.7,],
                                        [-0.2, 0,  0.05,],
                                        [ 0.2, 0,  0.05,]])
        # for surf in surfaces:
        #     surf_corners_body = []
        #     for corner in self.aircraft_surface_corners:
        #         surf_corners_body.append(surf.upoint + rotate_vector(surf.q, surf.pc+corner))
        #     surf_corners_body = np.array(surf_corners_body)
        #     xx, yy = np.meshgrid(surf_corners_body[:2, 0], surf_corners_body[1:3, 1])
        #     self.aircraft_surface_plots.append(mlab.mesh(xx, yy, surf_corners_body[:, 2].reshape(2, 2), colormap="autumn"))

        # Aliases for Mayavi animate decorator and show function
        self.animate = mlab.animate
        self.show = mlab.show

    def update(self, p, q, surfaces, view_kwargs={}):
        """
        Redraws the aircraft in fig according to the given position, quaternion, surfaces, and view.

        """
        # Transform body geometry to world coordinates (using rotation matrix is faster for multiple points)
        p = p.reshape(3, 1)
        R = rotmat_from_quaternion(q)
        aircraft_nodes_world = p + R.dot(self.aircraft_nodes)
        aircraft_fusel_world = p + R.dot(self.aircraft_fusel)
        aircraft_wings_world = p + R.dot(self.aircraft_wings)
        aircraft_tail_world = p + R.dot(self.aircraft_tail)

        # Update plot objects with new world coordinate information
        self.aircraft_nodes_plot.mlab_source.set(x=aircraft_nodes_world[0, :], y=aircraft_nodes_world[1, :], z=aircraft_nodes_world[2, :])
        self.aircraft_fusel_plot.mlab_source.set(x=aircraft_fusel_world[0, :], y=aircraft_fusel_world[1, :], z=aircraft_fusel_world[2, :])
        self.aircraft_wings_plot.mlab_source.set(x=aircraft_wings_world[0, :], y=aircraft_wings_world[1, :], z=aircraft_wings_world[2, :])
        self.aircraft_tail_plot.mlab_source.set(x=aircraft_tail_world[0, :], y=aircraft_tail_world[1, :], z=aircraft_tail_world[2, :])
        for i, surf in enumerate(surfaces):
            if i < 3:
                surf_corners_body = []
                for corner in self.aircraft_surface_corners:
                    surf_corners_body.append(surf.upoint + rotate_vector(surf.q, surf.pc+corner))
                surf_corners_body = np.array(surf_corners_body)
                surf_corners_world = (p + R.dot(surf_corners_body.T)).T
            # xx, yy = np.meshgrid(surf_corners_world[:2, 0], surf_corners_world[1:3, 1])
            # zz = np.vstack((surf_corners_world[:2, 2], surf_corners_world[:2, 2]))
            # self.aircraft_surface_plots[i].mlab_source.set(x=xx, y=yy, z=zz)
            if not hasattr(self, "ra_checker"):
                self.ra_checker = mlab.plot3d(surf_corners_world[:, 0], surf_corners_world[:, 1], surf_corners_world[:, 2])
            elif i==0:
                self.ra_checker.mlab_source.set(x=surf_corners_world[:, 0], y=surf_corners_world[:, 1], z=surf_corners_world[:, 2])
            if not hasattr(self, "la_checker"):
                self.la_checker = mlab.plot3d(surf_corners_world[:, 0], surf_corners_world[:, 1], surf_corners_world[:, 2])
            elif i==1:
                self.la_checker.mlab_source.set(x=surf_corners_world[:, 0], y=surf_corners_world[:, 1], z=surf_corners_world[:, 2])
            if not hasattr(self, "el_checker"):
                self.el_checker = mlab.plot3d(surf_corners_world[:, 0], surf_corners_world[:, 1], surf_corners_world[:, 2])
            elif i==2:
                self.el_checker.mlab_source.set(x=surf_corners_world[:, 0], y=surf_corners_world[:, 1], z=surf_corners_world[:, 2])
            if not hasattr(self, "ru_checker"):
                self.ru_checker = mlab.plot3d(surf_corners_world[:, 0], surf_corners_world[:, 1], surf_corners_world[:, 2])
            elif i==3:
                surf_corners_body = []
                for corner in self.rudder_corners:
                    surf_corners_body.append(surf.upoint + rotate_vector(surf.q, surf.pc+corner))
                surf_corners_body = np.array(surf_corners_body)
                surf_corners_world = (p + R.dot(surf_corners_body.T)).T
                self.ru_checker.mlab_source.set(x=surf_corners_world[:, 0], y=surf_corners_world[:, 1], z=surf_corners_world[:, 2])


        # Set camera view
        if view_kwargs: mlab.view(**view_kwargs)

###################################################################### INTERFACE SETUP

class Command(object):
    """
    Freedoms that would be commanded by a human pilot.

    """
    def __init__(self, thr=0.0, roll=0.0, pitch=0.0, yaw=0.0):
        self.thr = np.float64(thr)
        self.roll = np.float64(roll)
        self.pitch = np.float64(pitch)
        self.yaw = np.float64(yaw)

class Pilot(object):
    """
    User interface for remote-controlling.
    Call start_pilot_thread to begin filling an internal buffer with user input.
    Call get_command to execute / clear the buffer and get the current relevant Command object.
    Call stop_pilot_thread when done!

    max_thr:          magnitude of the largest acceptable throttle command
    max_roll:         magnitude of the largest acceptable roll command
    max_pitch:        magnitude of the largest acceptable pitch command
    max_yaw:          magnitude of the largest acceptable yaw command
    stick_deadband:   fraction of analog joystick travel that should be treated as zero
    trigger_deadband: fraction of analog trigger travel that should be treated as zero
    max_buffer_size:  maximum number of user commands that should be stored before dropping old ones
    button_callbacks: dictionary of callback functions keyed by button names (A, B, X, Y, L, R, SL, SR, DV, DH, K)

    """
    def __init__(self, max_thr=1, max_roll=1, max_pitch=1, max_yaw=1,
                 stick_deadband=0.1, trigger_deadband=0.0, max_buffer_size=200, button_callbacks={}):
        self.max_thr = np.float64(max_thr)
        self.max_roll = np.float64(max_roll)
        self.max_pitch = np.float64(max_pitch)
        self.max_yaw = np.float64(max_yaw)
        self.stick_deadband = float(stick_deadband)
        self.trigger_deadband = float(trigger_deadband)
        self.max_buffer_size = int(max_buffer_size)
        self.button_callbacks = button_callbacks

        # Valid input device names in priority order
        self.valid_device_names = ["Microsoft X-Box One pad (Firmware 2015)",
                                   "PowerA Xbox One wired controller",
                                   "Logitech Gamepad F310"]

        # Set valid input device
        self.input_device = None
        for valid_device_name in self.valid_device_names:
            if self.input_device is not None: break
            for device in devices:
                if device.name == valid_device_name:
                    self.input_device = device.name
                    print "Hello, Pilot! Ready to read from {}.".format(device.name)
                    break
        if self.input_device is None: raise IOError("FATAL: No valid input device is connected!")

        # Digital button code names
        self.button_codes = {"BTN_SOUTH": "A", "BTN_EAST": "B", "BTN_NORTH": "X", "BTN_WEST": "Y",
                             "BTN_TL": "L", "BTN_TR": "R", "BTN_SELECT": "SL", "BTN_START": "SR",
                             "ABS_HAT0Y": "DV", "ABS_HAT0X": "DH", "BTN_MODE": "K"}

        # Analog input characteristics
        self.max_stick = 32767
        if self.input_device == "Logitech Gamepad F310": self.max_trigger = 255
        else: self.max_trigger = 1023
        self.min_stick = int(self.stick_deadband * self.max_stick)
        self.min_trigger = int(self.trigger_deadband * self.max_trigger)

        # Internals
        self.command = None
        self.pilot_thread = None
        self.stay_alive = False
        self.buffer = deque([])
        self.buffer_size_flag = False

    def get_command(self):
        """
        Executes / clears the input buffer and returns the current relevant Command object.

        """
        if self.pilot_thread is None: raise AssertionError("FATAL: Cannot get_command without active pilot thread!")
        while self.buffer:
            event = self.buffer.pop()
            if event.code == "ABS_Y": pass
            elif event.code == "ABS_X": self.command.roll = self._stick_frac(event.state) * self.max_roll
            elif event.code == "ABS_RY": self.command.pitch = -self._stick_frac(event.state) * self.max_pitch
            elif event.code == "ABS_RX": self.command.yaw = self._stick_frac(event.state) * self.max_yaw
            elif event.code == "ABS_Z": pass
            elif event.code == "ABS_RZ": self.command.thr = self._trigger_frac(event.state) * self.max_thr
            elif event.code in self.button_codes:
                # if event.code == "BTN_WEST": self.command.start = int(event.state * self.mission_code)
                # elif event.code == "BTN_NORTH": self.command.cancel = bool(event.state)
                # elif event.code == "BTN_MODE": self.command.kill = bool(event.state)
                self.button_callbacks.get(self.button_codes[event.code], lambda val: None)(event.state)
        return self.command

    def start_pilot_thread(self):
        """
        Starts a thread that reads user input into the internal buffer.

        """
        if self.stay_alive:
            print "----------"
            print "WARNING: Pilot thread already running!"
            print "Cannot start another."
            print "----------"
            return
        self.command = Command()
        self.stay_alive = True
        if self.input_device in ["Microsoft X-Box One pad (Firmware 2015)",
                                 "PowerA Xbox One wired controller",
                                 "Logitech Gamepad F310"]:
            self.pilot_thread = Thread(target=self._listen_xbox)
        else:
            raise IOError("FATAL: No listener function has been implemented for device {}.".format(self.input_device))
        print "Pilot thread has begun!"
        self.pilot_thread.start()

    def stop_pilot_thread(self):
        """
        Terminates the Pilot's user input reading thread and clears the buffer.

        """
        self.stay_alive = False
        if self.pilot_thread is not None:
            print "Pilot thread terminating on next input!"
            self.pilot_thread.join()  # stay secure
            self.pilot_thread = None
        while self.buffer:
            self.buffer.pop()
        self.buffer_size_flag = False
        self.command = None

    def _listen_xbox(self):
        try:
            while self.stay_alive:
                self.buffer.appendleft(get_gamepad()[0])  # this is blocking (hence need for threading)
                if len(self.buffer) > self.max_buffer_size:
                    if not self.buffer_size_flag:
                        self.buffer_size_flag = True
                        print "----------"
                        print "WARNING: Pilot input buffer reached {} entries.".format(self.max_buffer_size)
                        print "Dropping old commands."
                        print "----------"
                    self.buffer.pop()
        finally:
            print "Pilot thread terminated!"
            self.pilot_thread = None

    def _stick_frac(self, val):
        if abs(val) > self.min_stick:
            return np.divide(val, self.max_stick, dtype=np.float64)
        return np.float64(0)

    def _trigger_frac(self, val):
        if abs(val) > self.min_trigger:
            return np.divide(val, self.max_trigger, dtype=np.float64)
        return np.float64(0)

# User button-press callbacks
####
# Toggle camera following
def bcb_A(val):
    global cam_follow
    if val:
        if cam_follow: cam_follow = False
        else: cam_follow = True

# Reset
def bcb_B(val):
    global state0, fixwing, des_roll, des_pitch, des_yaw, des_w0, des_w1, des_w2, rc, integ_roll, integ_pitch
    if val:
        fixwing.p = state0[0]
        fixwing.q = state0[1]
        fixwing.v = state0[2]
        fixwing.w = state0[3]
        des_roll = 0
        des_pitch = 0
        des_yaw = 0
        des_w0 = 0
        des_w1 = 0
        des_w2 = 0
        rc = np.array([0, 0, 0])
        integ_roll = 0
        integ_pitch = 0

# Zoom-out camera
def bcb_L(val):
    global cam_dist_rate
    cam_dist_rate = val*20  # m/s

# Zoom-in camera
def bcb_R(val):
    global cam_dist_rate
    cam_dist_rate = -val*20  # m/s

# # Decrement mission code
# def bcb_SL(val):
#     pilot.mission_code -= int(val)
#     if pilot.mission_code < 0: pilot.mission_code = 0
#     if val: print "Prepared for mission {}.".format(pilot.mission_code)

# # Increment mission code
# def bcb_SR(val):
#     pilot.mission_code += int(val)
#     if val: print "Prepared for mission {}.".format(pilot.mission_code)

# Change camera elevation
def bcb_DV(val):
    global cam_elev_rate
    cam_elev_rate = val*45  # deg/s

# Change camera azimuth
def bcb_DH(val):
    global cam_azim_rate
    cam_azim_rate = val*45  # deg/s
####

###################################################################### SIMULATION

# Time and timestep
t = time.time()  # s
dt = 0.01  # s

# Aircraft, scene, and user
fixwing = FixWing()
state0 = [fixwing.p, fixwing.q, fixwing.v, fixwing.w]
viz = Viz(fixwing.surfaces)
pilot = Pilot(button_callbacks={"A": bcb_A, "B": bcb_B, "L": bcb_L, "R": bcb_R, "DV": bcb_DV, "DH": bcb_DH})
                                # "SL": bcb_SL, "SR": bcb_SR, "DV": bcb_DV, "DH": bcb_DH})

# Initial camera condition
cam_state = {"focalpoint": fixwing.p.tolist(), "azimuth": 180, "elevation": 85, "distance": 25}  # m and deg
cam_azim_rate = 0
cam_elev_rate = 0
cam_dist_rate = 0
cam_follow = True

# Adaptive estimate of CoP, integrators, smoothers etc...
rc = np.array([0, 0, 0])
integ_roll = 0
integ_pitch = 0
des_roll = 0
des_pitch = 0
des_yaw = 0
des_w0 = 0
des_w1 = 0
des_w2 = 0
use_controller = 0
use_tgen = True
use_course = False

# Simulation loop function
@viz.animate(delay=50)  # ms (20 FPS is the best Mayavi can do)
def simulate():
    global cam_state, t, rc, integ_roll, integ_pitch, des_roll, des_pitch, des_yaw, des_w0, des_w1, des_w2
    while True:

        # Between each scene render, simulate up to real-time
        while t < time.time():

            # Update user input commands and compute efforts needed to achieve those commands
            cmd = pilot.get_command()
            lim = np.deg2rad(60)
            roll, pitch, yaw = euler_from_quaternion(fixwing.q)
            if use_tgen:  # whether or not to smooth inputs
                spring = 0.8
                damp = 2*np.sqrt(spring)
                des_a0 = spring*(cmd.roll*lim - des_roll) - damp*des_w0
                des_w0 += dt*des_a0
                des_roll += dt*des_w0 + 0.5*dt**2*des_a0
                des_a1 = spring*(cmd.pitch*lim - des_pitch) - damp*des_w1
                des_w1 += dt*des_a1
                des_pitch += dt*des_w1 + 0.5*dt**2*des_a1
                if use_course:
                    des_a2 = 5*(-np.deg2rad(10)*cmd.yaw - des_w2)
                    des_w2 += dt*des_a2
                    des_yaw += dt*des_w2 + 0.5*dt**2*des_a2
                    des_yaw = unwrap_angle(des_yaw)
                else:
                    des_yaw = yaw
                    des_w2 = -cmd.yaw*np.deg2rad(10)
            else:
                des_roll = cmd.roll*lim
                des_pitch = cmd.pitch*lim
                des_yaw = yaw
                des_w0 = des_w1 = 0
                des_w2 = -cmd.yaw*np.deg2rad(10)
            e = rotvec_from_quaternion(quaternion_multiply(quaternion_inverse(fixwing.q), quaternion_from_euler(des_roll, des_pitch, des_yaw)))
            kp = [20, 10, 10]
            kd = [10, 10, 10]
            if not use_course: kp[2] = 0
            if use_controller == 1:  # simple PD
                uroll = kp[0]*(des_roll - roll) + kd[0]*(des_w0 - fixwing.w[0])
                upitch = kp[1]*(des_pitch - pitch) + kd[1]*(des_w1 - fixwing.w[1])
                uyaw = kd[2]*(des_w2 - fixwing.w[2])
                fixwing.update(cmd.thr, uroll, upitch, -uyaw, t, dt)
                print " e: ", np.rad2deg(np.round(e, 3))
            elif use_controller == 2:  # PID
                uroll = kp[0]*(des_roll - roll) + kd[0]*(des_w0 - fixwing.w[0]) + integ_roll
                upitch = kp[1]*(des_pitch - pitch) + kd[1]*(des_w1 - fixwing.w[1]) + integ_pitch
                uyaw = kd[2]*(des_w2 - fixwing.w[2])
                integ_roll += dt*1*(des_roll - roll)
                integ_pitch += dt*3*(des_pitch - pitch)
                fixwing.update(cmd.thr, uroll, upitch, -uyaw, t, dt)
                print "integs roll, pitch: ", np.round((integ_roll, integ_pitch), 2), "| e: ", np.round(np.rad2deg(e), 1)
            elif use_controller == 3:  # adaptive
                Cp = fixwing.Cp1
                s = fixwing.v
                E = np.array([1, 1, 1])
                ff = (-dens*np.cross(rc, Cp*s) - np.cross(fixwing.w, fixwing.M.dot(fixwing.w)))# / (dens*E*npl.norm(s)**2)
                uroll = kp[0]*e[0] + kd[0]*(des_w0 - fixwing.w[0]) - ff[0]
                upitch = kp[1]*e[1] + kd[1]*(des_w1 - fixwing.w[1]) - ff[1]
                uyaw = kp[2]*e[2] + kd[2]*(des_w2 - fixwing.w[2]) - ff[2]
                Y = dens*np.array([[          0, -Cp[2]*s[2],  Cp[1]*s[1]],
                                   [ Cp[2]*s[2],           0, -Cp[0]*s[0]],
                                   [-Cp[1]*s[1],  Cp[0]*s[0],           0]])
                rc = rc - dt*0.03*([1, 1, 1]*Y.T.dot(kp*e + kd*([des_w0, des_w1, des_w2] - fixwing.w)))
                # rc = [-0.019, 0, 0]
                # print "ff: ", np.round(ff, 3)
                print "rc: ", np.round(rc, 3), "| e: ", np.round(np.rad2deg(e), 1)
                fixwing.update(cmd.thr, uroll, upitch, -uyaw, t, dt)
            else:  # direct inputs
                fixwing.update(cmd.thr, cmd.roll, cmd.pitch, cmd.yaw, t, dt)
            # print np.round(np.rad2deg(roll), 3), np.round(np.rad2deg(pitch), 3), np.round(np.rad2deg(yaw), 3)
            # fixwing.w = np.array([des_w0, des_w1, des_w2]) # IDEAL OVERRIDE
            # fixwing.q = quaternion_from_euler(des_roll, des_pitch, 0)
            t += dt

            # Update camera state according to user input
            if cam_follow: cam_state["focalpoint"] = fixwing.p.tolist()
            cam_state["azimuth"] += dt*cam_azim_rate
            cam_state["elevation"] += dt*cam_elev_rate
            cam_state["distance"] = np.clip(cam_state["distance"] + dt*cam_dist_rate, 5, np.inf)

        # Re-render changed parts of the scene at this real-time instant
        viz.update(fixwing.p, fixwing.q, fixwing.surfaces, cam_state)
        yield
        # print fixwing.surfaces[0].u, fixwing.surfaces[1].u


# Start'er up
pilot.start_pilot_thread()
simulate()
viz.show()  # blocking

# Be nice
pilot.stop_pilot_thread()
