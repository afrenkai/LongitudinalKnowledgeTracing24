#!/bin/bash
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --mem=128g
#SBATCH -J "MODEL_TYPEWYYEARFOLD_NUM"
#SBATCH -p short
#SBATCH -t 24:00:00
#SBATCH --gres=gpu:1
#SBATCH -C "H200"
module load cuda
source ~/myenvs/cuda-torch/bin/activate
python3 ../run_deep_model_within_year_one_fold.py MODEL_TYPE YEAR FOLD_NUM