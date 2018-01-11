import argparse
import json
import os
from pprint import pprint

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import gym
import pybullet_envs

import machina as mc
from machina.pols import GaussianPol
from machina.algos import trpo
from machina.prepro import BasePrePro
from machina.vfuncs import NormalizedDeterministicVfunc, DeterministicVfunc
from machina.envs import GymEnv
from machina.data import GAEData
from machina.samplers import BatchSampler
from machina.misc import logger
from net import PolNet, VNet

parser = argparse.ArgumentParser()
parser.add_argument('--log', type=str, default='garbage')
parser.add_argument('--env_name', type=str, default='Pendulum-v0')
parser.add_argument('--roboschool', action='store_true', default=False)
parser.add_argument('--record', action='store_true', default=False)
parser.add_argument('--episode', type=int, default=1000000)
parser.add_argument('--seed', type=int, default=256)
parser.add_argument('--max_episodes', type=int, default=1000000)

parser.add_argument('--max_samples_per_iter', type=int, default=5000)
parser.add_argument('--max_episodes_per_iter', type=int, default=250)
parser.add_argument('--epoch_per_iter', type=int, default=5)
parser.add_argument('--batch_size', type=int, default=64)
parser.add_argument('--pol_lr', type=float, default=1e-4)
parser.add_argument('--vf_lr', type=float, default=3e-4)
parser.add_argument('--normalize_v', action='store_true', default=False)
parser.add_argument('--use_prepro', action='store_true', default=False)

parser.add_argument('--gamma', type=float, default=0.995)
parser.add_argument('--lam', type=float, default=1)
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

if args.roboschool:
    import roboschool

logger.add_tabular_output(os.path.join(args.log, 'progress.csv'))

env = GymEnv(args.env_name, log_dir=os.path.join(args.log, 'movie'), record_video=args.record)
env.env.seed(args.seed)

ob_space = env.observation_space
ac_space = env.action_space

pol_net = PolNet(ob_space, ac_space)
pol = GaussianPol(ob_space, ac_space, pol_net)
vf_net = VNet(ob_space)
if args.normalize_v:
    vf = NormalizedDeterministicVfunc(ob_space, vf_net)
else:
    vf = DeterministicVfunc(ob_space, vf_net)
prepro = BasePrePro(ob_space)
sampler = BatchSampler(env)
optim_vf = torch.optim.Adam(vf_net.parameters(), args.vf_lr)

total_epi = 0
total_step = 0
max_rew = -1e6
while args.max_episodes > total_epi:
    if args.use_prepro:
        paths = sampler.sample(pol, args.max_samples_per_iter, args.max_episodes_per_iter, prepro.prepro_with_update)
    else:
        paths = sampler.sample(pol, args.max_samples_per_iter, args.max_episodes_per_iter)
    logger.record_tabular_misc_stat('Reward', [np.sum(path['rews']) for path in paths])
    data = GAEData(paths, shuffle=True)
    data.preprocess(vf, args.gamma, args.lam, centerize=True)
    result_dict = trpo.train(data, pol, vf, optim_vf, args.epoch_per_iter, args.batch_size)
    for key, value in result_dict.items():
        if not hasattr(value, '__len__'):
            logger.record_tabular(key, value)
        else:
            logger.record_tabular_misc_stat(key, value)
    total_epi += data.num_epi
    logger.record_tabular('EpisodePerIter', data.num_epi)
    logger.record_tabular('TotalEpisode', total_epi)
    step = sum([len(path['rews']) for path in paths])
    total_step += step
    logger.record_tabular('StepPerIter', step)
    logger.record_tabular('TotalStep', total_step)
    logger.dump_tabular()

    mean_rew = np.mean([np.sum(path['rews']) for path in paths])
    if mean_rew > max_rew:
        torch.save(pol.state_dict(), os.path.join(args.log, 'models', 'pol_max.pkl'))
        torch.save(vf.state_dict(), os.path.join(args.log, 'models', 'vf_max.pkl'))
        torch.save(optim_vf.state_dict(), os.path.join(args.log, 'models', 'optim_vf_max.pkl'))
        max_rew = mean_rew

    torch.save(pol.state_dict(), os.path.join(args.log, 'models', 'pol_last.pkl'))
    torch.save(vf.state_dict(), os.path.join(args.log, 'models', 'vf_last.pkl'))
    torch.save(optim_vf.state_dict(), os.path.join(args.log, 'models', 'optim_vf_last.pkl'))
    del data




