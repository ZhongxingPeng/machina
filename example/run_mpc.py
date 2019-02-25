"""
An example of Model Predictive Control.
"""

import argparse
import json
import os
from pprint import pprint

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import gym

import machina as mc
from machina.pols import GaussianPol, CategoricalPol, MultiCategoricalPol, MPCPol, RandomPol
from machina.algos import mpc
from machina.vfuncs import DeterministicSVfunc
from machina.models import DeterministicSModel
from machina.envs import GymEnv, C2DEnv
from machina.traj import Traj
from machina.traj import epi_functional as ef
from machina.traj import traj_functional as tf
from machina.samplers import EpiSampler
from machina import logger
from machina.utils import set_device, measure

from simple_net import PolNet, VNet, ModelNet, PolNetLSTM, VNetLSTM, ModelNetLSTM


def add_noise_to_init_obs(epis, std):
    with torch.no_grad():
        for epi in epis:
            epi['obs'][0] += np.random.normal(0, std, epi['obs'][0].shape)
    return epis


def rew_func(next_obs, acs):
    # HarfCheetah
    index_of_velx = 3
    if isinstance(next_obs, np.ndarray):
        rews = next_obs[:, index_of_velx] - 0.01 * \
            np.sum(acs**2, axis=1)
        rews = rews[0]
    else:
        rews = next_obs[:, index_of_velx] - 0.01 * \
            torch.sum(acs**2, dim=1)
        rews = rews.squeeze(0)

    return rews


parser = argparse.ArgumentParser()
parser.add_argument('--log', type=str, default='garbage')
parser.add_argument('--env_name', type=str, default='HalfCheetahBulletEnv-v0')
parser.add_argument('--c2d', action='store_true', default=False)
parser.add_argument('--pybullet_env', action='store_true', default=True)
parser.add_argument('--record', action='store_true', default=False)
parser.add_argument('--seed', type=int, default=256)
parser.add_argument('--num_parallel', type=int, default=4)
parser.add_argument('--cuda', type=int, default=-1)
parser.add_argument('--data_parallel', action='store_true', default=False)

parser.add_argument('--num_random_rollouts', type=int, default=60)
parser.add_argument('--noise_to_init_obs', type=float, default=0.001)
parser.add_argument('--n_samples', type=int, default=300)
parser.add_argument('--horizon_of_samples', type=int, default=4)
parser.add_argument('--num_aggregation_iters', type=int, default=1000)
parser.add_argument('--max_episodes_per_iter', type=int, default=9)
parser.add_argument('--epoch_per_iter', type=int, default=60)
parser.add_argument('--batch_size', type=int, default=64)
parser.add_argument('--dm_lr', type=float, default=1e-3)
parser.add_argument('--rnn', action='store_true', default=False)
args = parser.parse_args()

if not os.path.exists(args.log):
    os.mkdir(args.log)

with open(os.path.join(args.log, 'args.json'), 'w') as f:
    json.dump(vars(args), f)
pprint(vars(args))

if not os.path.exists(os.path.join(args.log, 'models')):
    os.mkdir(os.path.join(args.log, 'models'))

np.random.seed(args.seed)
torch.manual_seed(args.seed)

device_name = 'cpu' if args.cuda < 0 else "cuda:{}".format(args.cuda)
device = torch.device(device_name)
set_device(device)

if args.pybullet_env:
    import pybullet_envs

score_file = os.path.join(args.log, 'progress.csv')
logger.add_tabular_output(score_file)

env = GymEnv(args.env_name, log_dir=os.path.join(
    args.log, 'movie'), record_video=args.record)
env.env.seed(args.seed)
if args.c2d:
    env = C2DEnv(env)

ob_space = env.observation_space
ac_space = env.action_space

random_pol = RandomPol(ob_space, ac_space)

######################
### Model-Based RL ###
######################

### Prepare the dataset D_RAND ###

# Performing rollouts to collect training data
rand_sampler = EpiSampler(
    env, random_pol, num_parallel=args.num_parallel, seed=args.seed)

epis = rand_sampler.sample(random_pol, max_episodes=args.num_random_rollouts)
epis = add_noise_to_init_obs(epis, args.noise_to_init_obs)
traj = Traj()
traj.add_epis(epis)
traj = ef.add_next_obs(traj)
traj = ef.compute_h_masks(traj)
# obs, next_obs, and acs should become mean 0, std 1
traj, mean_obs, std_obs, mean_acs, std_acs = ef.normalize_obs_and_acs(traj)
traj.register_epis()

del rand_sampler

### Train Dynamics Model ###

# initialize dynamics model and mpc policy
if args.rnn:
    dm_net = ModelNetLSTM(ob_space, ac_space)
else:
    dm_net = ModelNet(ob_space, ac_space)
dm = DeterministicSModel(ob_space, ac_space, dm_net, args.rnn,
                         data_parallel=args.data_parallel, parallel_dim=1 if args.rnn else 0)
mpc_pol = MPCPol(ob_space, ac_space, dm_net, rew_func,
                 args.n_samples, args.horizon_of_samples,
                 mean_obs, std_obs, mean_acs, std_acs, args.rnn)
optim_dm = torch.optim.Adam(dm_net.parameters(), args.dm_lr)

rl_sampler = EpiSampler(
    env, mpc_pol, num_parallel=args.num_parallel, seed=args.seed)

# train loop
total_epi = 0
total_step = 0
counter_agg_iters = 0
max_rew = -1e-6
while args.num_aggregation_iters > counter_agg_iters:
    with measure('train model'):
        result_dict = mpc.train_dm(
            traj, dm, optim_dm, epoch=args.epoch_per_iter, batch_size=args.batch_size)
    with measure('sample'):
        mpc_pol = MPCPol(ob_space, ac_space, dm.net, rew_func,
                         args.n_samples, args.horizon_of_samples,
                         mean_obs, std_obs, mean_acs, std_acs, args.rnn)
        epis = rl_sampler.sample(
            mpc_pol, max_episodes=args.max_episodes_per_iter)

        curr_traj = Traj()
        curr_traj.add_epis(epis)

        curr_traj = ef.add_next_obs(curr_traj)
        curr_traj = ef.compute_h_masks(curr_traj)
        traj = ef.normalize_obs_and_acs(
            curr_traj, mean_obs, std_obs, mean_acs, std_acs, return_statistic=False)
        curr_traj.register_epis()
        traj.add_traj(curr_traj)

    total_epi += curr_traj.num_epi
    step = curr_traj.num_step
    total_step += step
    rewards = [np.sum(epi['rews']) for epi in epis]
    mean_rew = np.mean(rewards)
    logger.record_results(args.log, result_dict, score_file,
                          total_epi, step, total_step,
                          rewards,
                          plot_title=args.env_name)

    if mean_rew > max_rew:
        torch.save(dm.state_dict(), os.path.join(
            args.log, 'models', 'dm_max.pkl'))
        torch.save(optim_dm.state_dict(), os.path.join(
            args.log, 'models', 'optim_dm_max.pkl'))
        max_rew = mean_rew

    torch.save(dm.state_dict(), os.path.join(
        args.log, 'models', 'dm_last.pkl'))
    torch.save(optim_dm.state_dict(), os.path.join(
        args.log, 'models', 'optim_dm_last.pkl'))

    counter_agg_iters += 1
    del curr_traj
del rl_sampler
