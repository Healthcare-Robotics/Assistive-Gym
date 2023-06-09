import os, sys, multiprocessing, gym, ray, shutil, argparse, importlib, glob
import pickle
import time

import numpy as np
from cma import CMA, CMAEvolutionStrategy
from numpngw import write_apng
import pybullet as p
from matplotlib import pyplot as plt


def uniform_sample(pos, radius, num_samples):
    """
    Sample points uniformly from the given space
    :param pos: (x, y, z)
    :return:
    """
    # pos = np.array(pos)
    # points = np.random.uniform(low=pos-radius, high=pos + radius, size=(num_samples, 3))
    points = []
    for i in range(num_samples):
        r = np.random.uniform(radius / 2, radius)
        theta = np.random.uniform(0, np.pi / 2)
        phi = np.random.uniform(0, np.pi / 2)  # Only sample from 0 to pi/2

        # Convert from spherical to cartesian coordinates
        dx = r * np.sin(phi) * np.cos(theta)
        dy = r * np.sin(phi) * np.sin(theta)
        dz = r * np.cos(phi)

        # Add to original point
        x_new = pos[0] + dx
        y_new = pos[1] + dy
        z_new = pos[2] + dz
        points.append([x_new, y_new, z_new])
    return points


def inverse_dynamic(human):
    pos = []
    default_vel = 0.1
    for j in human.all_joint_indices:
        if p.getJointInfo(human.body, j, physicsClientId=human.id)[2] != p.JOINT_FIXED:
            joint_state = p.getJointState(human.body, j)
            pos.append(joint_state[0])

    # need to pass flags=1 to overcome inverseDynamics error for floating base
    # see https://github.com/bulletphysics/bullet3/issues/3188
    ivd = p.calculateInverseDynamics(human.body, objPositions= pos, objVelocities=[default_vel]*len(pos),
                                                 objAccelerations=[0] * len(pos), physicsClientId=human.id, flags=1)
    print ("inverse_dynamic: ", ivd, np.array(ivd).sum())
    return ivd


def draw_point(point, size=0.01):
    sphere = p.createCollisionShape(p.GEOM_SPHERE, radius=size)
    multiBody = p.createMultiBody(baseMass=0,
                                  baseCollisionShapeIndex=sphere,
                                  basePosition=np.array(point))
    p.setGravity(0, 0, 0)


def make_env(env_name, coop=False, seed=1001):
    if not coop:
        env = gym.make('assistive_gym:' + env_name)
    else:
        module = importlib.import_module('assistive_gym.envs')
        env_class = getattr(module, env_name.split('-')[0] + 'Env')
        env = env_class()
    env.seed(seed)
    return env


def solve_ik(env, target_pos, end_effector="right_hand"):
    human = env.human
    ee_idx = human.human_dict.get_dammy_joint_id(end_effector)
    ik_joint_indices = human.find_ik_joint_indices()
    # print ("ik_joint_indices: ", ik_joint_indices)
    solution = human.ik(ee_idx, target_pos, None, ik_joint_indices,  max_iterations=1000)  # TODO: Fix index
    # print ("ik solution: ", solution)
    return solution


def get_link_positions(env):
    human = env.human
    link_pos = []
    for i in range(-1, p.getNumJoints(human.body)): # include base
        pos, orient = human.get_pos_orient(i)
        link_pos.append(pos)
    return link_pos

def cal_energy_change(env, original_link_positions, current_link_positions):
    g = -9.81  # gravitational acceleration
    human_id = env.human.body
    total_energy_change = 0

    # Get the number of joints
    num_joints = p.getNumJoints(human_id)

    # Iterate over all links
    for i in range(-1, num_joints):

        # Get link state
        if i == -1:
            # The base case
            # state = p.getBasePositionAndOrientation(human_id)
            # velocity = p.getBaseVelocity(human_id)
            mass = p.getDynamicsInfo(human_id, -1)[0]
        else:
            # state = p.getLinkState(human_id, i)
            # velocity = p.getLinkState(human_id, i, computeLinkVelocity=1)[6:8]
            mass = p.getDynamicsInfo(human_id, i)[0]
        # Calculate initial potential energy
        potential_energy_initial = mass * g * original_link_positions[i][2] # z axis
        potential_energy_final = mass * g * current_link_positions[i][2]
        # Add changes to the total energy change
        total_energy_change += potential_energy_final - potential_energy_initial
    print(f"Total energy change: {total_energy_change}")

    return total_energy_change

def cost_fn(env, solution, target_pos, end_effector="right_hand", is_self_collision = False, is_env_collision= False,  energy_change = 0):
    human = env.human

    real_pos = p.getLinkState(human.body, human.human_dict.get_dammy_joint_id(end_effector))[0]
    dist = eulidean_distance(real_pos, target_pos)
    m = human.cal_chain_manipulibility(solution, end_effector)

    cost = dist + 1/m + -energy_change/100
    if is_self_collision:
        cost+=10
    if is_env_collision:
        cost+=10
    print("euclidean distance: ", dist, "manipubility: ", m, "cost: ", cost)

    return cost, m, dist


def eulidean_distance(cur, target):
    print("current: ", cur, "target: ", target)
    # convert tuple to np array
    cur = np.array(cur)
    return np.sqrt(np.sum(np.square(cur - target)))


# for debugging
def get_single_target(ee_pos):
    point = np.array(list(ee_pos))
    point[1] -= 0.2
    point[0] += 0.2
    point[2] += 0.2
    return point


def generate_target_points(env, num_points=10):
    # init points
    # human_pos = p.getBasePositionAndOrientation(env.human.body, env.human.id)[0]
    # points = uniform_sample(human_pos, 0.5, 20)
    human = env.human
    right_hand_pos = p.getLinkState(human.body, human.human_dict.get_dammy_joint_id("right_hand"))[0]
    points = uniform_sample(right_hand_pos, 0.5, num_points)
    return points


def get_initial_guess(env, target=None):
    if target is None:
        return np.zeros(len(env.human.controllable_joint_indices))  # no of joints
    else:
        # x0 = env.human.ik_chain(target)
        x0 = solve_ik(env, target, end_effector="right_hand")
        print("x0: ", x0)
        return x0


def debug_solution():
    # ee_pos, _, _= env.human.fk(["right_hand_limb"], x0)
    # ee_pos = env.human.fk_chain(x0)
    # print("ik error 2: ", eulidean_distance(ee_pos, target))
    # env.human.set_joint_angles(env.human.controllable_joint_indices, x0)

    # right_hand_ee = env.human.human_dict.get_dammy_joint_id("right_hand")
    # ee_positions, _ = env.human.forward_kinematic([right_hand_ee], x0)
    # print("ik error: ", eulidean_distance(ee_positions[0], target))
    #
    # for _ in range(1000):
    #     p.stepSimulation()
    # time.sleep(100)

    pass

def plot(vals, title, xlabel, ylabel):
    plt.figure()
    plt.plot(vals)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.show()


def plot_CMAES_metrics(mean_cost, mean_dist, mean_m):
    # Plot the fitness values
    plot(mean_cost, "Cost Function", "Iteration", "Cost")

    # Plot the distance values
    plot(mean_dist, "Distance Values", "Iteration", "Distance")

    # Plot the manipubility values
    plot(mean_m, "Manipubility Values", "Iteration", "Manipubility")

def test_collision():
    # print("collision1: ", human.check_self_collision())
    # # print ("collision1: ", perform_collision_check(human))
    # x1 = np.random.uniform(-1, 1, len(human.controllable_joint_indices))
    # human.set_joint_angles(human.controllable_joint_indices, x1)
    # # for i in range (100):
    # #     p.stepSimulation(physicsClientId=human.id)
    # #     print("collision2: ", human.check_self_collision())
    # p.performCollisionDetection(physicsClientId=human.id)
    # # print("collision2: ", perform_collision_check(human))
    # print("collision2: ", human.check_self_collision())
    # time.sleep(100)
    pass

def plot_mean_evolution(mean_evolution):
    # Plot the mean vector evolution
    mean_evolution = np.array(mean_evolution)
    plt.figure()
    for i in range(mean_evolution.shape[1]):
        plt.plot(mean_evolution[:, i], label=f"Dimension {i + 1}")
    plt.xlabel("Iteration")
    plt.ylabel("Mean Value")
    plt.title("Mean Vector Evolution")
    plt.legend()
    plt.show()


def step_forward(env, x0):
    p.setJointMotorControlArray(env.human.body, jointIndices=env.human.controllable_joint_indices, controlMode=p.POSITION_CONTROL,
                                forces=[1000] * len(env.human.controllable_joint_indices),
                                positionGains = [0.01] * len(env.human.controllable_joint_indices),
                                targetPositions=x0,
                                physicsClientId=env.human.id)
    # for _ in range(5):
    #     p.stepSimulation(physicsClientId=env.human.id)
    p.setRealTimeSimulation(1)

def train(env_name, algo, timesteps_total=10, save_dir='./trained_models/', load_policy_path='', coop=False, seed=0,
          extra_configs={}):
    env = make_env(env_name, coop=True)
    env.render()
    env.reset()

    # init points
    points = generate_target_points(env)
    pickle.dump(points, open("points.pkl", "wb"))

    actions = {}
    best_action_idx = 0
    best_cost = 10 ^ 9
    cost = 0
    env_object_ids= [env.robot.body, env.furniture.body, env.plane.body]
    human = env.human
    for (idx, target) in enumerate(points):
        draw_point(target, size=0.01)
        original_joint_angles = human.get_joint_angles(human.controllable_joint_indices)
        original_link_positions = get_link_positions(env)
        original_self_collisions = human.check_self_collision()
        original_collisions = human.check_env_collision(env_object_ids)

        x0 = get_initial_guess(env, None)
        optimizer = init_optimizer(x0, sigma=0.1)

        timestep = 0


        mean_cost = []
        mean_dist = []
        mean_m = []
        mean_evolution = []


        while not optimizer.stop():
            timestep += 1
            solutions = optimizer.ask()
            fitness_values = []
            dists = []
            manipus = []
            energy_changes = []
            for s in solutions:

                human.set_joint_angles(human.controllable_joint_indices, s)  # force set joint angle
                cur_link_positions = get_link_positions(env)
                inverse_dynamic(human)

                self_collisions = human.check_self_collision()
                is_self_collision = len(self_collisions) > len(original_self_collisions)
                env_collisions = human.check_env_collision(env_object_ids)
                is_env_collision = len(env_collisions) > len(original_collisions)
                print ("self collision: ", is_self_collision, "env collision: ", env_collisions, "is env collision: ", is_env_collision)

                energy_change = cal_energy_change(env, original_link_positions, cur_link_positions)
                cost, m, dist= cost_fn(env, s, target, is_self_collision=is_self_collision, is_env_collision=is_env_collision, energy_change = energy_change)

                # restore joint angle
                human.set_joint_angles(human.controllable_joint_indices, original_joint_angles)

                fitness_values.append(cost)
                dists.append(dist)
                manipus.append(m)
                energy_changes.append(energy_change)
            optimizer.tell(solutions, fitness_values)
            print("timestep: ", timestep, "cost: ", cost)
            optimizer.result_pretty()

            mean_evolution.append(np.mean(solutions, axis=0))
            mean_cost.append(np.mean(fitness_values, axis=0))
            mean_dist.append(np.mean(dists, axis=0))
            mean_m.append(np.mean(manipus, axis=0))

        env.human.set_joint_angles(env.human.controllable_joint_indices, optimizer.best.x)
        actions[idx] = optimizer.best.x

        plot_CMAES_metrics(mean_cost, mean_dist, mean_m)
        plot_mean_evolution(mean_evolution)

        if cost < best_cost:
            best_cost = cost
            best_action_idx = idx

    env.disconnect()
    # save action to replay
    # print("actions: ", len(actions))
    # pickle.dump(actions, open("actions.pkl", "wb"))
    # pickle.dump(best_action_idx, open("best_action_idx.pkl", "wb"))

    return env, actions


def init_optimizer(x0, sigma):
    opts = {}
    opts['tolfun']=  1e-3
    opts['tolx'] = 1e-3
    es = CMAEvolutionStrategy(x0, sigma, opts)
    return es


def render(env, actions):
    # print("actions: ", actions)
    env.render()  # need to call reset after render
    env.reset()

    # init points
    points = pickle.load(open("points.pkl", "rb"))
    best_idx = pickle.load(open("best_action_idx.pkl", "rb"))
    for (idx, point) in enumerate(points):
        print(idx, point)

        if idx == best_idx:
            draw_point(point, size=0.05)
        else:
            draw_point(point)
    for a in actions[best_idx]:
        env.step(a)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='RL for Assistive Gym')
    parser.add_argument('--env', default='ScratchItchJaco-v0',
                        help='Environment to train.py on (default: ScratchItchJaco-v0)')
    parser.add_argument('--algo', default='ppo',
                        help='Reinforcement learning algorithm')
    parser.add_argument('--seed', type=int, default=1,
                        help='Random seed (default: 1)')
    parser.add_argument('--train', action='store_true', default=False,
                        help='Whether to train.py a new policy')
    parser.add_argument('--render', action='store_true', default=False,
                        help='Whether to render a single rollout of a trained policy')
    parser.add_argument('--evaluate', action='store_true', default=False,
                        help='Whether to evaluate a trained policy over n_episodes')
    parser.add_argument('--train-timesteps', type=int, default=10,
                        help='Number of simulation timesteps to train.py a policy (default: 1000000)')
    parser.add_argument('--save-dir', default='./trained_models/',
                        help='Directory to save trained policy in (default ./trained_models/)')
    parser.add_argument('--load-policy-path', default='./trained_models/',
                        help='Path name to saved policy checkpoint (NOTE: Use this to continue training an existing policy, or to evaluate a trained policy)')
    parser.add_argument('--render-episodes', type=int, default=1,
                        help='Number of rendering episodes (default: 1)')
    parser.add_argument('--eval-episodes', type=int, default=100,
                        help='Number of evaluation episodes (default: 100)')
    parser.add_argument('--colab', action='store_true', default=False,
                        help='Whether rendering should generate an animated png rather than open a window (e.g. when using Google Colab)')
    parser.add_argument('--verbose', action='store_true', default=False,
                        help='Whether to output more verbose prints')
    args = parser.parse_args()

    coop = ('Human' in args.env)
    checkpoint_path = None

    if args.train:
        _, actions = train(args.env, args.algo, timesteps_total=args.train_timesteps, save_dir=args.save_dir,
                           load_policy_path=args.load_policy_path, coop=coop, seed=args.seed)

    if args.render:
        actions = pickle.load(open("actions.pkl", "rb"))
        env = make_env(args.env, coop=True)
        render(env, actions)