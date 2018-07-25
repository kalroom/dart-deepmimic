__author__ = 'anish'

import numpy as np
from gym import utils
from gym.envs.dart import dart_env
import copy

import baselines.common.tf_util as U
from baselines.ppo1.mlp_policy import MlpPolicy

import pydart2 as pydart
import argparse
from amc import AMC
from asf_skeleton import ASF_Skeleton
from joint import expand_angle, compress_angle
from transformations import quaternion_from_euler, euler_from_quaternion
from transformations import compose_matrix, euler_from_matrix
import math

class StateMode:

    GEN_EULER = 0
    GEN_QUAT = 1
    GEN_AXIS = 2

    MIX_EULER = 3
    MIX_QUAT = 4
    MIX_AXIS = 5


class ActionMode:

    GEN_EULER = 0
    GEN_QUAT = 1
    GEN_AXIS = 2

    lengths = [3, 4, 4]

ROOT_THETA_KEY = "root_theta"
ROOT_POS_KEY = "root_pos"
# Unlike the two vars above, ROOT_KEY isn't customizeable. It should correspond
# with whatever the root node is named in the amc (which should always be
# "root" anyways)
ROOT_KEY = "root"

def get_metadict(amc_frame, skel_dofs, asf):
    """
    @type amc_frame: A dictionary mapping dof names -> mocap data
    @type skel_dofs: The array of skeleton dofs, as given by skel.dofs
    @type asf: An instance of the ASFSkeleton class

    @return: A dictionary which maps dof names WHICH APPEAR IN MOCAP DATA
    to tuples where:
        - the first element is the list of indices the dof occupies in skel_dofs
        - the second element is the joint's angle order (a string such as "xz"
          or "zyx")
    """
    # TODO Find a way of extracting information other than
    # relying on the underlying amc file
    # dof_data is dof_name -> (range of indices, order )

    # TODO README EMERGENCY!!!
    # If the output of this function is ever changed so that the number of
    # actuated dofs is no longer given by (size of output dict - 1), then
    # the setting of action_dim in DartDeepMimic will need to be updated

    dof_data = {}
    for dof_name, _ in amc_frame:
        joint = asf.name2joint[dof_name]
        axes_str = joint.dofs
        indices = [i for i, dof in enumerate(skel_dofs)
                   if dof.name.startswith(dof_name)]
        dof_data[dof_name] = (indices, axes_str)

    return dof_data

class DartDeepMimic(dart_env.DartEnv):

    def __init__(self, control_skeleton_path, asf_path,
                 reference_motion_path,
                 statemode = StateMode.GEN_EULER,
                 actionmode = StateMode.GEN_EULER,
                 visualize=True, frame_skip=16, dt=.005,
                 max_action_magnitude=10,
                 screen_width=80,
                 screen_height=45):

        self.statemode = statemode
        self.actionmode = actionmode

        self.frame = 0
        self.old_frame = 0

        ###########################################################
        # Extract dof info so that states can be converted easily #
        ###########################################################

        world = pydart.World(dt, control_skeleton_path)
        asf = ASF_Skeleton(asf_path)

        self.ref_skel = world.skeletons[1]
        self.amc = AMC(reference_motion_path)
        self.metadict = get_metadict(self.amc.frames[0],
                                      self.ref_skel.dofs, asf)
        # Setting control skel to ref skel is just a workaround:
        # it's set to its correct value later on
        self.control_skel = self.ref_skel

        ################################################
        # Do some calculations related to action space #
        ################################################

        # Calculate the size of the neural network output vector
        self.action_dim = ActionMode.lengths[actionmode] \
                          * (len(self.metadict) - 1)
        self.action_limits = [max_action_magnitude * np.ones(self.action_dim),
                              -max_action_magnitude * np.ones(self.action_dim)]


        # TODO Parse end effectors from the ASF or Skel, idc which #

        dart_env.DartEnv.__init__(self, [control_skeleton_path], frame_skip,
                                  len(self._get_obs()),
                                  self.action_limits, dt, "parameter",
                                  "continuous", visualize, not visualize)

        self.control_skel = self.dart_world.skeletons[1]

        ##################################
        # Simulation stuff for DeepMimic #
        ##################################
        self.frame = 0
        self.old_frame = 0
        self.old_skelq = self.control_skel.q

        self.P = .5 * np.ndarray(self.control_skel.num_dofs())
        self.D = .1 * np.ndarray(self.control_skel.num_dofs())

        self._curr_mocap_frame = 0
        self._past_mocap_frame = self._curr_mocap_frame

    @property
    def curr_mocap_frame(self):
        return self._curr_mocap_frame

    @curr_mocap_frame.setter
    def curr_mocap_frame(self, new):

        new = new % len(self.amc.frames)

        if new == self._curr_mocap_frame + 1:
            self._past_mocap_frame += 1
        else:
            self._past_mocap_frame = new

        self._curr_mocap_frame = new

    def sync_skel_to_frame(self, skel, frame_index):
        """
        Given a skeleton reference, use the metadict (assumed to be correct)
        to sync all the dofs
        """
        frame = self.amc.frames[frame_index]
        # TODO Move conversions somewhere else...

        def sequential_degrees_to_rotating_radians(rvector):

            rvector = np.multiply(rvector, math.pi / 180)

            rmatrix = compose_matrix(angles=rvector, angle_order="sxyz")
            return euler_from_matrix(rmatrix[:3, :3], axes="rxyz")

        def map_dofs(dof_list, pos_list):
            for dof, pos in zip(dof_list, pos_list):
                dof.set_position(float(pos))

        # World to root joint is a bit special so we handle it here...
        root_data = frame[0][1]
        map_dofs(skel.dofs[3:6], root_data[:3])
        map_dofs(skel.dofs[0:3],
                 sequential_degrees_to_rotating_radians(root_data[3:]))

        # And handle the rest of the dofs normally
        for joint_name, joint_angles in frame[1:]:
            dof_indices, order = self.metadict[joint_name]
            start_index, end_index = dof_indices[0], dof_indices[-1]

            # AMC data is in sequential degrees while Dart expects rotating
            # radians, so we do some conversion here

            # TODO Write a converter which will export the amc angles to be in
            # the propert format ahead of time rather than perform expensive
            # computations all the time down here

            # TODO Hold on... how is it so good while totally failing to
            # account for joint dof order or number? This might be the cause of
            # the weird foot-moving syndrome...
            theta = expand_angle(joint_angles, order)
            rotation_euler = sequential_degrees_to_rotating_radians(theta)
            new_rotation_euler = compress_angle(rotation_euler, order)
            # print(str(rotation_euler) + " " + order + "\n --> " + str(new_rotation_euler))
            # print(order, rotation_euler)

            map_dofs(skel.dofs[start_index : end_index + 1],
                     new_rotation_euler)

    def _get_obs(self):
        """
        Return a 1-dimensional vector of the skeleton's state, as defined by
        the state code
        """

        if self.statemode == StateMode.GEN_EULER:
            return self.gencoords_as_euler(self.control_skel)
        elif self.statemode == StateMode.GEN_QUAT:
            return self.gencoords_as_quat(self.control_skel)
        elif self.statemode == StateMode.GEN_AXIS:
            return lambda: self.gencoords_as_axisangle(self.control_skel)
        else:
            raise RuntimeError("Unrecognized or unimpletmented state code: "
                               + str(statemode))


    def q_components_euler(self, generalized_q):
        """
        @type generalized_q: A vector of dof values, as given by skel.q or .dq

        @return: A tuple where
            - index 0 is a 3-array representing the translational component of
              generalized q
            - [1] is a dictionary mapping dof names to of 3-arrays representing
              euler angles; the first index of an element is the x-coordinate,
              the second is y, 3rd is z
        @type: Tuple
        """

        root_translation = generalized_q[3:6]
        expanded_angles = {}
        expanded_angles[ROOT_THETA_KEY] = expand_angle(generalized_q[0:3],
                                                       "xyz")
        for dof_name in self.metadict:
            if dof_name == ROOT_KEY:
                continue
            indices, order = self.metadict[dof_name]
            fi = indices[0]
            li = indices[-1]
            expanded_angles[dof_name] = expand_angle(generalized_q[fi:li],
                                                     order)
        return root_translation, expanded_angles

    def gencoords_of_skel(self, skeleton):
        """
        @type skeleton: A dart skeleton

        You're not meant to try and extract information on specific dofs from
        this, it's basically returns intermediate ready to be flattened and
        passed to a neural network

        @return: A tuple containing information on both q and dq
            - [0] is a list of 2 3-arrays representing the translational parts
                  - [0] is the pos, [1] is the velocity
            - [1] is a list of 3-arrays representing the angular parts
                  - Elements in the first half are angles, the second half is
                    angular velocities
        @type: Tuple(list of 2 3-arrays, list of 3-arrays)
        """

        pos, angles_dict = self.q_components_euler(skeleton.q)
        dpos, dangles_dict = self.q_components_euler(skeleton.dq)

        angles = np.array(list(angles_dict.values()))
        dangles = np.array(list(dangles_dict.values()))

        gen_pos = np.array([pos, dpos])
        gen_angles = np.concatenate([angles, dangles])

        return gen_pos, gen_angles

    def flatten_components(self, components):
        """
        @param gencoords: A tuple formatted similarly to that returned by
        gencoords_of_skel. However the angles may be converted into a different
        format, such as quaternions
            - [1] This parameter (corresponding to the angular information)
              should be a LIST not a dictionary

        @return: A flattened 1-dimensional array suitable for passing into a NN
        """
        positional, angular = components
        pos_flat = np.concatenate(positional)
        ang_flat = np.concatenate(angular)
        return np.concatenate([pos_flat, ang_flat])

    def gencoords_as_quat(self, skel):
        """
        Return a state vector where anglular quantities are in
        quaternion format
        """
        pos, ang = self.gencoords_of_skel(skel)
        ang = [quaternion_from_euler(*t, axes="rxyz") for t in ang]
        return self.flatten_components((pos, ang))

    def gencoords_as_euler(self, skel):
        """
        Return a state vector where anglular quantities are in
        euler format
        """
        pos, ang = self.gencoords_of_skel(skel)
        return self.flatten_components((pos, ang))

    def gencoords_as_axisangle(self, skel):
        """
        Return a state vector where anglular quantities are in
        axis-angle format
        """
        pos, ang = self.gencoords_of_skel(skel)
        ang = [axisangle_from_euler(*t, axes="rxyz") for t in ang]
        return self.flatten_components((pos, ang))

    def _target_angles(self, raw_action):
        """
        Given a 1-dimensional vector representing a neural network output,
        construct from it a set of targets for the ACTUATED degrees of freedom
        (ie the ones in metadict, minus the root)

        Because of how action_dim is defined up in __init__, raw_action
        should always have the correct dimensions
        """

        output_angles = np.reshape(raw_action,
                                   (-1, ActionMode.lengths[self.actionmode]))

        # TODO Normalize and validate values to make sure they're actually
        # valid euler angles / quaternions / whatever
        if self.actionmode == ActionMode.GEN_EULER:
            return output_angles
        elif self.actionmode == ActionMode.GEN_QUAT:
            return [euler_from_quaternion(*t, axes="rxyz")
                    for t in output_angles]
        elif self.actionmode == ActionMode.GEN_AXIS:
            return [euler_from_axisangle(*t, axes="rxyz")
                    for t in output_angles]
        else:
            raise RuntimeError("Unrecognized or unimpletmented action code: "
                               + str(actionmode))

    # def euler_to_acutated_q(self, angles):
    #     """
    #     Given a list of 3-tuples representing target euler angles for the
    #     ACTUATED degrees of freedom, use the skeleton metadict to compress them
    #     into a single vector (where angles have been truncated based on how
    #     many degrees of freedom the relevant joint has)
    #     """

    #     q = np.zeros(self.control_skel.num_dofs())
    #     i = 0
    #     for key in self.metadict:
    #         if key == ROOT_THETA_KEY:
    #             continue
    #         indices = self.metadict[key][0]
    #         f, l = indices[0], indices[-1] + 1
    #         theta = compress_angle(angles[i],
    #                                self.metadict[key][1])
    #         q[f:l] = theta
    #         i += 1

    #     return q[6:]

    def reward(self, old_skelq, new_skelq):
        raise NotImplementedError()

    def advance(self, a):
        raise NotImplementedError()
        # clamped_control = np.array(a)

        # self.tau = np.zeros(self.robot_skeleton.ndofs)
        # trans = np.zeros(6,)


        # self.target[6:]=  self.transformActions(clamped_control)# + self.WalkPositions[self.count,6:] #*self.action_scale# + self.ref_trajectory_right[self.count_right,6:]# +

        # self.target[[6,9,10]] = self.target[[12,15,16]]
        # actions = np.zeros(29,)
        # actions[2] = 5
        # actions[6:] = copy.deepcopy(self.target[6:])
        # self.action_skel.set_positions(actions)
        # self.action_skel.set_velocities(np.zeros(29,))

        # for i in range(4):
        #     self.tau[6:] = self.PID()

        #     dupq = copy.deepcopy(self.WalkPositions[self.count,:])
        #     dupq[0] = 0.90
        #     dupq[2] = 0.5

        #     self.dupSkel.set_positions(dupq)

        #     dupdq = np.zeros(29,)
        #     dupdq = copy.deepcopy(self.WalkVelocities[self.count,:])

        #     self.dupSkel.set_velocities(dupdq)

        #     if self.dumpTorques:
        #         with open("torques.txt","ab") as fp:
        #             np.savetxt(fp,np.array([self.tau]),fmt='%1.5f')

        #     if self.dumpActions:
        #         with open("targets_from_net.txt",'ab') as fp:
        #             np.savetxt(fp,np.array([[self.target[6],self.robot_skeleton.q[6]]]),fmt='%1.5f')


        #     self.robot_skeleton.set_forces(self.tau)
        #     #print("torques",self.tau[22])
        #     self.dart_world.step()

        #self.do_simulation(self.tau, self.frame_skip)

    def ClampTorques(self,torques):
        raise NotImplementedError()

    def torques_by_pd(self, target_angles, current_angles,
                      past_angles):
        """
        Given target, current, and past angles (all lists of 3-vectors
        representing fully-specified euler angles) of the actuated dofs, return
        torques for the WHOLE SHEBANG.

        This method returns a vector of torques for EVERY DOF in the entire
        skeleton. This means that it takes care of compressing angles to their
        respective orders, placing them in the right spots in a vector of size
        skel.num_dofs, etc

        Non actuated dofs will of course have torques of 0
        """
        current_error = target_angles - current_angles
        past_error = target_angles - past_angles

        error_rate = (current_error - past_error) / self.dt

        # compression phase
        actuated_dof_names = [key for key in self.metadict
                              if key != ROOT_KEY]
        projected_current_error = [compress_angle(current_error[i],
                                                  self.metadict[key][1])
                                   for i, key in enumerate(actuated_dof_names)]

        projected_error_rate = [compress_angle(error_rate[i],
                                               self.metadict[key][1])
                                for i, key in enumerate(actuated_dof_names)]

        exp_current_error = np.zeros(self.control_skel.num_dofs())
        exp_error_rate = np.zeros(self.control_skel.num_dofs())

        for index, key in enumerate(actuated_dof_names):
            dof_indices = self.metadict[key][0]
            f, l = dof_indices[0], dof_indices[-1] + 1
            exp_current_error[f:l] = projected_current_error[index]

        for index, key in enumerate(actuated_dof_names):
            dof_indices = self.metadict[key][0]
            f, l = dof_indices[0], dof_indices[-1] + 1
            exp_error_rate[f:l] = projected_error_rate[index]

        # TODO it would be nice to only specify P and D for the parameters
        # which are actuated, but such is life I guess
        return self.P * exp_current_error + self.D * exp_error_rate

    def step(self, a):

        actuation_targets = self._target_angles(a)

        _, current_euler = self.q_components_euler(self.control_skel.q)
        actuated_angles = np.array([current_euler[key]
                                  for key in current_euler
                                  if key != ROOT_THETA_KEY])

        _, old_euler = self.q_components_euler(self.old_skelq)
        old_actuated_angles = np.array([current_euler[key]
                                        for key in old_euler
                                        if key != ROOT_THETA_KEY])

        torques = self.torques_by_pd(actuation_targets,
                                     actuated_angles,
                                     old_actuated_angles)

        self.old_skelq = self.control_skel.q

        # TODO Clamp torques?
        # Also what is the difference between world step
        self.control_skel.set_forces(torques)
        self.dart_world.step()
        self.do_simulation(torques, self.frame_skip)

        newstate = self._get_obs()
        reward = 4
        done = False
        extrainfo = {}

        return newstate, reward, done, extrainfo
        # self.dart_world.set_text = []
        # self.dart_world.y_scale = np.clip(a[6],-2,2)
        # self.dart_world.plot = False
        # count_str = "count :"+str(self.count)
        # a_from_net = "a[6] : %f and a[12] : %f"%(a[16],a[20])
        # self.dart_world.set_text.append(a_from_net)
        # self.dart_world.set_text.append(count_str)
        # posbefore = self.robot_skeleton.bodynodes[0].com()[0]


        # self.advance(a)
        # if self.dumpActions:
        #     with open("a_from_net.txt","ab") as fp:
        #         np.savetxt(fp,np.array([a]),fmt='%1.5f')

        # #print("torques",self.tau[[6,12]])
        # point_rarm = [0.,-0.60,-0.15]
        # point_larm = [0.,-0.60,-0.15]
        # point_rfoot = [0.,0.,-0.20]
        # point_lfoot = [0.,0.,-0.20]

        # global_rarm = self.robot_skeleton.bodynodes[16].to_world(point_rarm)

        # global_larm = self.robot_skeleton.bodynodes[13].to_world(point_larm)
        # global_lfoot = self.robot_skeleton.bodynodes[4].to_world(point_lfoot)
        # global_rfoot = self.robot_skeleton.bodynodes[7].to_world(point_rfoot)

        # global_rarmdup = self.dupSkel.bodynodes[16].to_world(point_rarm)
        # global_larmdup = self.dupSkel.bodynodes[13].to_world(point_larm)
        # global_lfootdup = self.dupSkel.bodynodes[4].to_world(point_lfoot)
        # global_rfootdup = self.dupSkel.bodynodes[7].to_world(point_rfoot)

        # self.dart_world.contact_point = []
        # self.dart_world.contact_color = 'red'
        # self.dart_world.contact_point.append(global_rarm)
        # self.dart_world.contact_point.append(global_larm)
        # self.dart_world.contact_point.append(global_rfoot)
        # self.dart_world.contact_point.append(global_lfoot)
        # self.dart_world.contact_color = 'green'
        # self.dart_world.contact_point.append(global_rarmdup)
        # self.dart_world.contact_point.append(global_larmdup)
        # self.dart_world.contact_point.append(global_rfootdup)
        # self.dart_world.contact_point.append(global_lfootdup)


        # #print(self.swingFoot)
        # posafter = self.robot_skeleton.bodynodes[0].com()[0]
        # height = self.robot_skeleton.bodynodes[0].com()[1]
        # side_deviation = self.robot_skeleton.bodynodes[0].com()[2]

        # upward = np.array([0, 1, 0])
        # upward_world = self.robot_skeleton.bodynode('head').to_world(
        #     np.array([0, 1, 0])) - self.robot_skeleton.bodynode('head').to_world(np.array([0, 0, 0]))
        # upward_world /= np.linalg.norm(upward_world)
        # ang_cos_uwd = np.dot(upward, upward_world)
        # ang_cos_uwd = np.arccos(ang_cos_uwd)

        # forward = np.array([1, 0, 0])
        # forward_world = self.robot_skeleton.bodynode('head').to_world(
        #     np.array([1, 0, 0])) - self.robot_skeleton.bodynode('head').to_world(np.array([0, 0, 0]))
        # forward_world /= np.linalg.norm(forward_world)
        # ang_cos_fwd = np.dot(forward, forward_world)
        # ang_cos_fwd = np.arccos(ang_cos_fwd)

        # lateral = np.array([0, 0, 1])
        # lateral_world = self.robot_skeleton.bodynode('head').to_world(
        #     np.array([0, 0, 1])) - self.robot_skeleton.bodynode('head').to_world(np.array([0, 0, 0]))
        # lateral_world /= np.linalg.norm(lateral_world)
        # ang_cos_ltl = np.dot(lateral, lateral_world)
        # ang_cos_ltl = np.arccos(ang_cos_ltl)

        # contacts = self.dart_world.collision_result.contacts

        # if contacts == []:
        #     self.switch = 0

        # if contacts != []:
        #     self.switch = 1


        # self.ComputeReward()

        # total_force_mag = 0
        # for contact in contacts:


        #     force = np.sum(contact.force[[1,2]])



        #     total_force_mag += force
        #     data = np.zeros(11,)
        #     data[:10] = contact.state
        #     data[10] = self.count


        # if self.dumpCOM:
        #     with open("COM_fromPolicy_walk.txt","ab") as fp:
        #         np.savetxt(fp,np.asarray(self.robot_skeleton.q[:3]),fmt="%1.5f")

        #     with open("Joint_fromPolicy_walk.txt","ab") as fp:
        #         np.savetxt(fp,np.asarray(self.robot_skeleton.q),fmt="%1.5f")



        # alive_bonus = 4
        # vel = (posafter - posbefore) / self.dt
        # #print("a shape",a[-1])
        # action_pen = np.sqrt(np.square(a[:23]).sum())

        # reward = 0


        # W_joint = 2.
        # W_joint_vel = 2.
        # W_trans = 2.
        # W_orient = 1.

        # #W_theta = 0.5
        # W_joint_vel = 1.
        # W_trans_vel = 0.5
        # W_orient_vel = 0.5
        # W_balance = 5.0

        # Joint_weights = np.ones(23,)#
        # Joint_weights[[0,3,6,9,16,20,10,16]] = 10

        # Weight_matrix = np.diag(Joint_weights)
        # Weight_matrix_1 = np.diag(np.ones(4,))

        # veldq = np.copy(self.robot_skeleton.dq)
        # #print("ve",veldq)
        # acc = (veldq - self.prevdq)/0.008


        # com_height = np.square(0 - self.robot_skeleton.bodynodes[0].com()[1])
        # com_height_reward = 10*np.exp(-5*com_height)
        # right_foot = np.square(0 - self.robot_skeleton.bodynodes[7].com()[1])
        # right_foot_reward = 10*np.exp(-5*right_foot)

        # #print("rew",right_foot_reward)


        # done = False


        # rarm_term = np.sum(np.square(self.rarm_endeffector[self.count,:] - global_rarm))
        # larm_term = np.sum(np.square(self.larm_endeffector[self.count,:] - global_larm))
        # rfoot_term = np.sum(np.square(self.rfoot_endeffector[self.count,:] - global_rfoot))
        # lfoot_term = np.sum(np.square(self.lfoot_endeffector[self.count,:] - global_lfoot))

        # end_effector_reward = np.exp(-40*(rarm_term+larm_term+rfoot_term+lfoot_term))
        # com_reward = np.exp(-40*np.sum(np.square(self.com[self.count,:] - self.robot_skeleton.bodynodes[0].com())))

        # s = self.state_vector()







        # joint_diff = self.WalkPositions[self.count,6:] - self.robot_skeleton.q[6:]#hmm[[6,9,12,15,22,26,10,16]] - self.robot_skeleton.q[[6,9,12,15,22,26,10,16]]
        # #joint_diff_unimp = hmm[[7,8,13,14]] - self.robot_skeleton.q[[7,8,13,14]]
        # joint_pen = np.sum(joint_diff.T*Weight_matrix*joint_diff)
        # #joint_pen_unimp = np.sum(joint_diff_unimp.T*Weight_matrix_1*joint_diff_unimp)

        # vel_diff = self.WalkVelocities[self.count,6:] - self.robot_skeleton.dq[6:]

        # vel_pen = np.sum(vel_diff.T*Weight_matrix*vel_diff)

        # node1_trans = np.array([0,-0.25,0])
        # node1_root_orient = np.array([-np.pi/5,0,0])
        # node0_trans = self.qpos_node0[:3]

        # trans_pen = np.sum(np.square(node0_trans[:3] - self.robot_skeleton.q[:3]))
        # trans_vel_pen = np.sum(np.square(np.zeros(3,) - self.robot_skeleton.dq[:3]))
        # root_orient_pen = np.sum(np.square(np.zeros(3,) - self.robot_skeleton.q[3:6]))
        # root_orient_vel = np.sum(np.square(self.init_dq[3:6] - self.robot_skeleton.dq[3:6]))

        # #print("com",self.robot_skeleton.bodynodes[0].com())
        # ##node1
        # root_node_com = np.array([0,-0.10,0]) #
        # trans_pen = np.sum(np.square(root_node_com - self.robot_skeleton.bodynodes[0].com()))
        # trans_vel_pen = np.sum(np.square(self.robot_skeleton.dq[:3]))
        # root_orient_pen = np.sum(np.square(np.zeros(3,) - self.robot_skeleton.q[3:6]))
        # root_orient_vel = np.sum(np.square(self.init_dq[3:6] - self.robot_skeleton.dq[3:6]))
        # #orient_vel = np.copy(self.ref_vel[int(self.count/10),3:6])
        # #trans_vel = np.copy(self.ref_trajectory[int(self.count/10),[2,1,0]])
        # #trans_vel_pen = np.sum(np.square(trans_vel - self.robot_skeleton.dq[3:6]))
        # #joint_vel_pen = np.sum(np.square(self.ref_vel[int(self.count/10),6:18] - self.robot_skeleton.dq[6:18]))
        # root_trans_term = 10/(1+ 100*trans_pen)#np.asarray(W_trans*np.exp(-10*trans_pen))
        # #
        # root_trans_vel = 100/(1+ 100*trans_vel_pen)#np.asarray(W_joint*np.exp(-10*trans_vel_pen))
        # #
        # joint_term = 1*np.asarray(np.exp(-2e-1*joint_pen))#np.asarray(W_joint*np.exp(-1e-2*joint_pen)) #100
        # #joint_term_unimp = np.asarray(W_joint*np.exp(-joint_pen_unimp))
        # #
        # joint_vel_term = 1*np.asarray(np.exp(-1e-1*vel_pen))# W_joint_vel*np.exp(-1e-3*vel_pen)
        # #20 for root nod0
        # orient_term = 10*np.asarray(W_orient*np.exp(-10*root_orient_pen))

        # com_height = self.robot_skeleton.bodynodes[0].com()[1]
        # contact_reward = 0.
        # if self.count > 230 and self.count < 300 and contacts == []:
        #     contact_reward = 10.

        # quat_term = self.ComputeReward()
        # reward = 0.1*end_effector_reward + 0.1*joint_vel_term+ 0.25*com_reward+ 1.65*quat_term# + contact_reward#  + joint_term + joint_vel_term #0.1*self.robot_skeleton.bodynodes[0].com()[1] + joint_term + joint_vel_term +
        # eerew_str = "End Effector :"+str(end_effector_reward)
        # self.dart_world.set_text.append(eerew_str)

        # vel_str = "Joint Vel :"+str(joint_vel_term)
        # self.dart_world.set_text.append(vel_str)

        # com_str = "Com  :"+str(com_reward)
        # self.dart_world.set_text.append(com_str)

        # joint_str = "Joint :"+str(quat_term)
        # self.dart_world.set_text.append(joint_str)

        # joint_str = "contact :"+str(contact_reward)
        # self.dart_world.set_text.append(joint_str)

        # lthigh_str = "Left Thigh target:"+str(self.WalkPositions[self.count,6])+" thigh Position :"+str(self.robot_skeleton.q[6])
        # self.dart_world.set_text.append(lthigh_str)
        # rthigh_str = "right Thigh target:"+str(self.WalkPositions[self.count,12])+" thigh Position :"+str(self.robot_skeleton.q[12])
        # self.dart_world.set_text.append(rthigh_str)

        # lthigh_torque = "Left Thigh torque:"+str(self.tau[6])
        # self.dart_world.set_text.append(lthigh_torque)
        # rthigh_torque = "right Thigh torque:"+str(self.tau[12])
        # self.dart_world.set_text.append(rthigh_torque)

        # com_vel = "com_vel:"+str(self.robot_skeleton.q[1])
        # self.dart_world.set_text.append(com_vel)

        # tar_vel = "tar_com_vel:"+str(self.WalkVelocities[self.count,1])
        # self.dart_world.set_text.append(tar_vel)


        # c = 0
        # head_flag = False
        # for item in contacts:
        #     #c = 0
        #     if item.skel_id1 == 0:
        #         if self.robot_skeleton.bodynodes[item.bodynode_id2].name == "head":
        #             #print("Headddddddd")
        #             head_flag = True
        #         if self.robot_skeleton.bodynodes[item.bodynode_id2].name == "l-lowerarm":
        #             c+=1
        #         if self.robot_skeleton.bodynodes[item.bodynode_id2].name == "r-lowerarm":
        #             #print("true")
        #             c+=1
        #         if self.robot_skeleton.bodynodes[item.bodynode_id2].name == "r-foot":
        #             c+=1

        #         if self.robot_skeleton.bodynodes[item.bodynode_id2].name == "l-foot":
        #             c+=1






        # done = not (np.isfinite(s).all() and (np.abs(s[2:]) < 200).all() and# (abs(L_angle - self.foot_angles[self.count]) < 10) and (abs(R_angle - self.foot_angles[self.count]) < 10) and
        #         (height > -0.7) and  (self.robot_skeleton.q[3] > -0.4) and (self.robot_skeleton.q[3]<0.3) and (abs(self.robot_skeleton.q[4]) < 0.30) and (abs(self.robot_skeleton.q[5]) < 0.30))

        # flag = 0

        # if done:
        #     reward = 0.
        #     flag = 1


        # ob = self._get_obs()

        # if self.dumpStates:
        #     with open("states_from_net.txt","ab") as fp:
        #         np.savetxt(fp,np.array([ob]),fmt='%1.5f')
        # if self.trainRelay:
        #     ac,vpred = self.pol.act(False,ob)
        #     #print("vpred",vpred)
        #     if vpred > 4000:
        #         print("yipeee",vpred)
        #         reward =  10*vpred#/100
        #         done = True

        # if head_flag:
        #     reward = 0.
        #     done = True

        # self.prevdq = np.copy(self.robot_skeleton.dq)


        # self.t += self.dt


        # reward_breakup = {'r':np.array([flag])}#,-total_force_mag/1000., -1e-2*np.sum(self.robot_skeleton.dq[[6,12,9,15,10,16]]), 10*self.robot_skeleton.dq[2], 10*self.robot_skeleton.dq[1],flag])}#{'r':np.array([right_foot_reward])}#
        # if self.dumpRewards:
        #     with open("reward_terms.txt","ab") as fp:
        #         np.savetxt(fp,np.array([[root_trans_term,root_trans_vel,joint_term,orient_term,joint_vel_term,0.1*com_height_reward,flag]]),fmt="%1.5f")

        #     with open("reward.txt","ab") as fp:
        #         np.savetxt(fp,np.array([[reward]]),fmt='%1.5f')



        # self.prev_a = a
        # ob = self._get_obs()
        # joint_every_diff = np.sum(np.square(self.WalkPositions[:,6:] - self.robot_skeleton.q[6:]),axis=1)
        # min_error = np.argmin(joint_every_diff)
        # #print("joint joint_every_diff",min_error)
        # #self.count = min_error
        # self.count+=1
        # if self.count>= 322:#449
        #     done = True

        # self.dart_world.set_text.append(str(done))

        # return ob, reward, done,reward_breakup


    def reset_model(self):
        raise NotImplementedError()
        return self._get_obs()

    def viewer_setup(self):
        if not self.disableViewer:
            self._get_viewer().scene.tb.trans[0] = 5.0
            self._get_viewer().scene.tb.trans[2] = -30
            self._get_viewer().scene.tb.trans[1] = 0.0

    def render(self, mode='human', close=False):
            # if not self.disableViewer:
        if True:
            self._get_viewer().scene.tb.trans[0] = -self.dart_world.skeletons[self.track_skeleton_id].com()[0]*1
        if close:
            if self.viewer is not None:
                self._get_viewer().close()
                self.viewer = None
            return

        if mode == 'rgb_array':
            data = self._get_viewer().getFrame()
            return data
        elif mode == 'human':
            self._get_viewer().runSingleStep()

if __name__ == "__main__":


    parser = argparse.ArgumentParser(description='Make a DartDeepMimic Environ')
    parser.add_argument('--control-skel-path', required=True,
                        help='Path to the control skeleton')
    parser.add_argument('--asf-path', required=True,
                        help='Path to asf which the skeleton was parsed from')
    parser.add_argument('--ref-motion-path', required=True,
                        help='Path to the reference motion AMC')
    parser.add_argument('--state-mode', default=0, type=int,
                        help="Code for the state representation")
    parser.add_argument('--action-mode', default=0, type=int,
                        help="Code for the action representation")
    parser.add_argument('--visualize', default=True,
                        help="True if you want a window to render to")
    parser.add_argument('--frame-skip', type=int, default=16,
                        help="IDK what this does")
    parser.add_argument('--dt', type=float, default=.002,
                        help="Dart simulation resolution")
    parser.add_argument('--window-width', type=int, default=80,
                        help="Window width")
    parser.add_argument('--window-height', type=int, default=45,
                        help="Window height")

    args = parser.parse_args()

    env = DartDeepMimic(args.control_skel_path, args.asf_path,
                        args.ref_motion_path,
                        args.state_mode, args.action_mode, args.visualize,
                        args.frame_skip, args.dt,
                        args.window_width, args.window_height)

    for i in range(200):
        env.sync_skel_to_frame(env.control_skel, 0)
        a = env.action_space.sample()
        # env.step(a)
        env.render()
