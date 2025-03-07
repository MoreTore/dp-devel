#!/usr/bin/env python3
import math
import numpy as np
from common.numpy_fast import clip, interp

import cereal.messaging as messaging
from common.conversions import Conversions as CV
from common.filter_simple import FirstOrderFilter
from common.realtime import DT_MDL
from selfdrive.modeld.constants import T_IDXS
from selfdrive.controls.lib.longcontrol import LongCtrlState
from selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, MIN_ACCEL, MAX_ACCEL, T_FOLLOW
from selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from selfdrive.controls.lib.drive_helpers import V_CRUISE_MAX, CONTROL_N, get_speed_error
from system.swaglog import cloudlog
from selfdrive.controls.lib.vision_turn_controller import VisionTurnController
from selfdrive.controls.lib.speed_limit_controller import SpeedLimitController, SpeedLimitResolver
from selfdrive.controls.lib.turn_speed_controller import TurnSpeedController
from selfdrive.controls.lib.events import Events

LON_MPC_STEP = 0.2  # first step is 0.2s
A_CRUISE_MIN = -1.2
A_CRUISE_MAX_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MAX_BP = [0., 10.0, 25., 40.]

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]

#DP_FOLLOWING_DIST = {
#  0: 1.0,
#  1: 1.2,
#  2: 1.4,
#  3: 1.8,
#}

DP_ACCEL_ECO = 0
DP_ACCEL_NORMAL = 1
DP_ACCEL_SPORT = 2

# accel profile by @arne182 modified by cgw
_DP_CRUISE_MIN_V =       [-0.53, -0.53, -0.55, -0.65, -0.65, -0.60,  -0.43]
_DP_CRUISE_MIN_V_ECO =   [-0.50, -0.50, -0.53, -0.60, -0.60, -0.55,  -0.40]
_DP_CRUISE_MIN_V_SPORT = [-0.60, -0.60, -0.65, -0.70, -0.80, -0.70,  -0.50]
_DP_CRUISE_MIN_BP =      [0.,    3.,    8.3,   14,    20.,   30.,   55.]
#DP_CRUISE_MIN_BP in mph=[0.,    2,     18,    31,    44,    67,    94]

_DP_CRUISE_MAX_V =       [3.5, 3.4, 2.1, 1.6, 1.1,  0.91, 0.69, 0.44, 0.34, 0.14]
_DP_CRUISE_MAX_V_ECO =   [3.0, 1.8, 1.3, 1.0, 0.71, 0.59, 0.45, 0.36, 0.28, 0.09]
_DP_CRUISE_MAX_V_SPORT = [3.5, 3.5, 3.4, 3.0, 2.1,  1.7,  1.3,  0.9,  0.7,  0.5]
_DP_CRUISE_MAX_BP =      [0.,  3,   6.,  8.,  11.,  15.,  20.,  25.,  30.,  42.]
#DP_CRUISE_MAX_BP in mph=[0.,  6.7, 13,  18,  25,   33,   45,   56,   67,   94]

# count n times before we decide a lead is there or not
_DP_E2E_LEAD_COUNT = 50
# lead distance
_DP_E2E_LEAD_DIST = 50

_DP_E2E_SNG_COUNT = 250

def dp_calc_cruise_accel_limits(v_ego, dp_profile):
  if dp_profile == DP_ACCEL_ECO:
    a_cruise_min = interp(v_ego, _DP_CRUISE_MIN_BP, _DP_CRUISE_MIN_V_ECO)
    a_cruise_max = interp(v_ego, _DP_CRUISE_MAX_BP, _DP_CRUISE_MAX_V_ECO)
  elif dp_profile == DP_ACCEL_SPORT:
    a_cruise_min = interp(v_ego, _DP_CRUISE_MIN_BP, _DP_CRUISE_MIN_V_SPORT)
    a_cruise_max = interp(v_ego, _DP_CRUISE_MAX_BP, _DP_CRUISE_MAX_V_SPORT)
  else:
    a_cruise_min = interp(v_ego, _DP_CRUISE_MIN_BP, _DP_CRUISE_MIN_V)
    a_cruise_max = interp(v_ego, _DP_CRUISE_MAX_BP, _DP_CRUISE_MAX_V)
  return a_cruise_min, a_cruise_max

def get_max_accel(v_ego):
  return interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)


def limit_accel_in_turns(v_ego, angle_steers, a_target, CP):
  """
  This function returns a limited long acceleration allowed, depending on the existing lateral acceleration
  this should avoid accelerating when losing the target in turns
  """

  # FIXME: This function to calculate lateral accel is incorrect and should use the VehicleModel
  # The lookup table for turns should also be updated if we do this
  a_total_max = interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
  a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))

  return [a_target[0], min(a_target[1], a_x_allowed)]


class LongitudinalPlanner:
  def __init__(self, CP, init_v=0.0, init_a=0.0):
    # dp - conditional e2e
    self.dp_e2e_has_lead = False
    self.dp_e2e_lead_last = False
    self.dp_e2e_lead_count = 0
    self.dp_e2e_mode_last = 'acc'
    self.dp_e2e_sng = False
    self.dp_e2e_sng_count = 0
    self.dp_e2e_standstill_last = False

    self.CP = CP
    self.mpc = LongitudinalMpc()
    self.fcw = False

    self.a_desired = init_a
    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, DT_MDL)
    self.v_model_error = 0.0

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)
    self.solverExecutionTime = 0.0

    # dp
    self.dp_accel_profile_ctrl = False
    self.dp_accel_profile = DP_ACCEL_ECO
    self.dp_following_profile_ctrl = False
    self.dp_following_profile = 0
    self.cruise_source = 'cruise'
    self.vision_turn_controller = VisionTurnController(CP)
    self.speed_limit_controller = SpeedLimitController()
    self.events = Events()
    self.turn_speed_controller = TurnSpeedController()
    self.dp_e2e_adapt_ap = False

  # dp - conditional e2e
  def conditional_e2e(self, standstill, within_speed_condition, e2e_lead, within_speed_condition_lead):
    reset_state = False

    # lead counter
    # to avoid lead comes and go too quickly causing mode switching too fast
    # we count _DP_E2E_LEAD_COUNT before we update lead existence.
    if e2e_lead != self.dp_e2e_lead_last:
      self.dp_e2e_lead_count = 0
    else:
      self.dp_e2e_lead_count += 1

      # when lead status count > _DP_E2E_LEAD_COUNT, we update actual lead status
      if self.dp_e2e_lead_count >= _DP_E2E_LEAD_COUNT:
        self.dp_e2e_has_lead = e2e_lead

    if not standstill and self.dp_e2e_standstill_last:
      self.dp_e2e_sng = True

    if self.dp_e2e_sng:
      self.dp_e2e_sng_count += 1
      if self.dp_e2e_sng_count >= _DP_E2E_SNG_COUNT:
        self.dp_e2e_sng = False
        self.dp_e2e_sng = 0

    dp_e2e_mode = 'acc'
    # standstill uses e2e, to prevent lead suddenly move away.
    if standstill:
      self.dp_e2e_sng = 0
      self.dp_e2e_sng = False
      dp_e2e_mode = 'blended'
    else:
      # lead is driving below x km/h
      if self.dp_e2e_has_lead:
        if within_speed_condition_lead:
          dp_e2e_mode = 'blended'
      else:
        # within speed condition and does not have a lead, use e2e
        if within_speed_condition:
          dp_e2e_mode = 'blended'

    self.mpc.mode = dp_e2e_mode
    if dp_e2e_mode != self.dp_e2e_mode_last:
      reset_state = True

    self.dp_e2e_lead_last = e2e_lead
    self.dp_e2e_mode_last = dp_e2e_mode
    self.dp_e2e_standstill_last = standstill

    return reset_state

  @staticmethod
  def parse_model(model_msg, model_error):
    if (len(model_msg.position.x) == 33 and
      len(model_msg.velocity.x) == 33 and
      len(model_msg.acceleration.x) == 33):
      x = np.interp(T_IDXS_MPC, T_IDXS, model_msg.position.x) - model_error * T_IDXS_MPC
      v = np.interp(T_IDXS_MPC, T_IDXS, model_msg.velocity.x) - model_error
      a = np.interp(T_IDXS_MPC, T_IDXS, model_msg.acceleration.x)
      j = np.zeros(len(T_IDXS_MPC))
    else:
      x = np.zeros(len(T_IDXS_MPC))
      v = np.zeros(len(T_IDXS_MPC))
      a = np.zeros(len(T_IDXS_MPC))
      j = np.zeros(len(T_IDXS_MPC))
    return x, v, a, j

  def get_df(self, v_ego):
    desired_tf = T_FOLLOW
    if not self.dp_e2e_adapt_fp and self.mpc.mode == 'blended':
      return desired_tf
    if self.dp_following_profile_ctrl:
      if self.dp_following_profile == 0:
        x_vel =  [1.1,  3.3,   5.5,    19.7,   22.0,   25.0,   42]
        y_dist = [1.0,  1.24,  1.31,   1.33,   1.3,    1.24,   1.25]
        desired_tf = np.interp(v_ego, x_vel, y_dist)
      elif self.dp_following_profile == 1:
        x_vel =  [1.1,  5.556, 19.7,   42]
        y_dist = [1.0,  1.35,  1.6,    1.6 ]
        desired_tf = np.interp(v_ego, x_vel, y_dist)
      elif self.dp_following_profile == 2:
        x_vel =  [1.1,  5.556, 19.7,   42]
        y_dist = [1.0,  1.4,   2.0,    2.0]
        desired_tf = np.interp(v_ego, x_vel, y_dist)
    return desired_tf

  def update(self, sm, read=True):
    # dp
    self.dp_accel_profile_ctrl = sm['dragonConf'].dpAccelProfileCtrl
    self.dp_accel_profile = sm['dragonConf'].dpAccelProfile
    self.dp_following_profile_ctrl = sm['dragonConf'].dpFollowingProfileCtrl
    self.dp_following_profile = sm['dragonConf'].dpFollowingProfile
    dp_reset_state = False

    if sm['dragonConf'].dpE2EConditional:
      self.dp_e2e_adapt_ap = sm['dragonConf'].dpE2EConditionalAdaptAp
      e2e_lead = sm['radarState'].leadOne.status and sm['radarState'].leadOne.dRel <= _DP_E2E_LEAD_DIST
      within_speed_condition = sm['controlsState'].vCruise <= sm['dragonConf'].dpE2EConditionalAtSpeed
      within_speed_condition_lead = (sm['radarState'].leadOne.vRel + sm['carState'].vEgo)*3.6 <= sm['dragonConf'].dpE2EConditionalAtSpeedLead
      if self.conditional_e2e(sm['carState'].standstill, within_speed_condition, e2e_lead, within_speed_condition_lead):
        dp_reset_state = True
    else:
      self.mpc.mode = 'blended' if sm['controlsState'].experimentalMode else 'acc'

    v_ego = sm['carState'].vEgo
    v_cruise_kph = sm['controlsState'].vCruise
    v_cruise_kph = min(v_cruise_kph, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off
    force_slow_decel = sm['controlsState'].forceDecel

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['controlsState'].enabled

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    if self.mpc.mode == 'acc':
      if self.dp_accel_profile_ctrl:
        accel_limits = dp_calc_cruise_accel_limits(v_ego, self.dp_accel_profile)
      else:
        accel_limits = [A_CRUISE_MIN, get_max_accel(v_ego)]
      accel_limits_turns = limit_accel_in_turns(v_ego, sm['carState'].steeringAngleDeg, accel_limits, self.CP)
    else:
      if sm['dragonConf'].dpE2EConditional and sm['dragonConf'].dpE2EConditionalAdaptAp and self.dp_accel_profile_ctrl:
        _, accel_max = dp_calc_cruise_accel_limits(v_ego, self.dp_accel_profile)
        accel_limits = [MIN_ACCEL, accel_max]
      else:
        accel_limits = [MIN_ACCEL, MAX_ACCEL]
      accel_limits_turns = [MIN_ACCEL, MAX_ACCEL]

    if reset_state or dp_reset_state:
      self.v_desired_filter.x = v_ego
      # Clip aEgo to cruise limits to prevent large accelerations when becoming active
      self.a_desired = clip(sm['carState'].aEgo, accel_limits[0], accel_limits[1])

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))
    # Compute model v_ego error
    self.v_model_error = get_speed_error(sm['modelV2'], v_ego)

    # Get acceleration and active solutions for custom long mpc.
    self.cruise_source, a_min_sol, v_cruise_sol = self.cruise_solutions(not reset_state, self.v_desired_filter.x,
                                                                        self.a_desired, v_cruise, sm)

    if force_slow_decel:
      v_cruise = 0.0
    # clip limits, cannot init MPC outside of bounds
    accel_limits_turns[0] = min(accel_limits_turns[0], self.a_desired + 0.05, a_min_sol)
    accel_limits_turns[1] = max(accel_limits_turns[1], self.a_desired - 0.05)

    # dp - mpc.set_weights calls moved to mpc.update function because we need lead0 and lead1 data
    # self.mpc.set_weights(prev_accel_constraint)
    self.mpc.set_accel_limits(accel_limits_turns[0], accel_limits_turns[1])
    self.mpc.set_cur_state(self.v_desired_filter.x, self.a_desired)
    x, v, a, j = self.parse_model(sm['modelV2'], self.v_model_error)
    self.mpc.update(sm['radarState'], v_cruise_sol, x, v, a, j, prev_accel_constraint, self.get_df(v_ego))

    self.v_desired_trajectory_full = np.interp(T_IDXS, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory_full = np.interp(T_IDXS, T_IDXS_MPC, self.mpc.a_solution)
    self.v_desired_trajectory = self.v_desired_trajectory_full[:CONTROL_N]
    self.a_desired_trajectory = self.a_desired_trajectory_full[:CONTROL_N]
    self.j_desired_trajectory = np.interp(T_IDXS[:CONTROL_N], T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Interpolate 0.05 seconds and save as starting point for next iteration
    a_prev = self.a_desired
    self.a_desired = float(interp(DT_MDL, T_IDXS[:CONTROL_N], self.a_desired_trajectory))
    self.v_desired_filter.x = self.v_desired_filter.x + DT_MDL * (self.a_desired + a_prev) / 2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState'])

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.status
    longitudinalPlan.longitudinalPlanSource = self.mpc.source if self.mpc.source != 'cruise' else self.cruise_source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.visionTurnControllerState = self.vision_turn_controller.state
    longitudinalPlan.visionTurnSpeed = float(self.vision_turn_controller.v_turn)

    longitudinalPlan.speedLimitControlState = self.speed_limit_controller.state
    longitudinalPlan.speedLimit = float(self.speed_limit_controller.speed_limit)
    longitudinalPlan.speedLimitOffset = float(self.speed_limit_controller.speed_limit_offset)
    longitudinalPlan.distToSpeedLimit = float(self.speed_limit_controller.distance)
    longitudinalPlan.isMapSpeedLimit = bool(self.speed_limit_controller.source == SpeedLimitResolver.Source.map_data)
    longitudinalPlan.eventsDEPRECATED = self.events.to_msg()

    longitudinalPlan.turnSpeedControlState = self.turn_speed_controller.state
    longitudinalPlan.turnSpeed = float(self.turn_speed_controller.speed_limit)
    longitudinalPlan.distToTurn = float(self.turn_speed_controller.distance)
    longitudinalPlan.turnSign = int(self.turn_speed_controller.turn_sign)

    longitudinalPlan.dpE2EIsBlended = self.mpc.mode == 'blended'

    pm.send('longitudinalPlan', plan_send)

  def cruise_solutions(self, enabled, v_ego, a_ego, v_cruise, sm):
    # Update controllers
    self.vision_turn_controller.update(enabled, v_ego, a_ego, v_cruise, sm)
    self.events = Events()
    self.speed_limit_controller.update(enabled, v_ego, a_ego, sm, v_cruise, self.events)
    self.turn_speed_controller.update(enabled, v_ego, a_ego, sm)

    # Pick solution with lowest velocity target.
    a_solutions = {'cruise': float("inf")}
    v_solutions = {'cruise': v_cruise}

    if self.vision_turn_controller.is_active:
      a_solutions['turn'] = self.vision_turn_controller.a_target
      v_solutions['turn'] = self.vision_turn_controller.v_turn

    if self.speed_limit_controller.is_active:
      a_solutions['limit'] = self.speed_limit_controller.a_target
      v_solutions['limit'] = self.speed_limit_controller.speed_limit_offseted

    if self.turn_speed_controller.is_active:
      a_solutions['turnlimit'] = self.turn_speed_controller.a_target
      v_solutions['turnlimit'] = self.turn_speed_controller.speed_limit

    source = min(v_solutions, key=v_solutions.get)

    return source, a_solutions[source], v_solutions[source]
