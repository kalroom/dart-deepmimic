
from cgkit.asfamc import AMCReader
import numpy as np
import math
from transformations import compose_matrix, euler_from_matrix
from skeleton import Skeleton

class AMC:
    """
    Parent class representing information from a .amc file
    """

    def __init__(self, amc_filename, skeleton):

        self.name = None
        self.frames = []
        self.skeleton = skeleton

        def __init_frame(framenum, data):
            self.frames.append(data)

        reader = AMCReader(amc_filename)
        reader.onFrame = __init_frame
        reader.read()

    def sync_angles(self, framenum):
        """
        Call this method to set all of the skeleton's angles to the values in the
        corresponding frame of the AMC (where 0 is the first frame)
        """
        raise NotImplementedError("Abstract class, use either ASF or SKEL AMC classes")

class ASF_AMC(AMC):

    def sync_angles(self, framenum):

        frame = self.frames[framenum]
        root_data = frame[0][1]
        self.skeleton.root.direction = np.array(root_data[0:3])
        self.skeleton.root.theta_degrees = np.array(root_data[3:])

        for bone_name, bone_data in frame[1:]:
            self.skeleton.name2bone[bone_name].set_theta_degrees(*bone_data)

def sequential_to_rotating_radians(rvector):

    rmatrix = compose_matrix(angles=rvector, angle_order="sxyz")
    return euler_from_matrix(rmatrix[:3, :3], axes="rxyz")


class Skel_AMC(AMC):
    """
    A class to sync AMC frames with a Dart Skeleton object
    """

    def __init__(self, amc_filename, skeleton, skeleton_filename):
        super(Skel_AMC, self).__init__(amc_filename, skeleton)

        # Set up a map of joint names to dof indices
        # start index and window length tuple
        self.joint2window = {}
        self.asf_skeleton = Skeleton(skeleton_filename)

        dof_names = [dof.name for dof in self.skeleton.dofs]

        for joint in self.skeleton.joints:
            i = 0
            while True:
                if skeleton.dofs[i].name[:len(joint.name)] == joint.name:
                    self.joint2window[joint.name] = (i, joint.num_dofs())
                    break
                i += 1

    def sync_angles(self, framenum):

        # framenum = 0

        frame = self.frames[framenum]
        root_data = frame[0][1]

        def zip_dofs(dof_list, pos_list):

            for dof, pos in zip(dof_list, pos_list):
                dof.set_position(pos)

        zip_dofs(self.skeleton.dofs[0:3],
                 sequential_to_rotating_radians(np.multiply(math.pi / 180,
                                                            root_data[3:])))
        zip_dofs(self.skeleton.dofs[3:6], root_data[:3])

        for bone_name, bone_data in frame[1:]:
            index, length = self.joint2window[bone_name]

            # I need this to take advantage of the auto-angle placement
            asf_bone = self.asf_skeleton.name2bone[bone_name]
            asf_bone.set_theta_degrees(*bone_data)
            rotation_euler = sequential_to_rotating_radians(asf_bone.theta_radians)

            zip_dofs(self.skeleton.dofs[index : index + length],
                     rotation_euler)
